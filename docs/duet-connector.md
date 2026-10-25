# Duet connector pack

The duet pack connects Claude Duet, a hosted agent that runs tasks on its own
cloud machine, to Brigade memory, skills and the Fleet Hub inbox. It runs the
Dot connector code under a separate profile, so it shares Dot's origin
verification, schemas and state handling but none of Dot's identity, inbox,
state or Access application.

Duet has no inbound task API. A claude.ai custom MCP connector is the
integration surface, and every Duet session on the account inherits it.

The default bind is `127.0.0.1:8780`. The registry `public_route` is empty.
Configure a dedicated public host and route `/mcp` through its own Cloudflare
Access application to this listener.

## What Duet can and cannot do

Duet does its work on its own cloud compute. The profile has no dispatch: no
tool can start, follow up, answer, approve or interrupt work on operator
machines, and no runtime key turns that on. These ten tools are absent from
tool discovery, not merely refused at call time:

`fleet_submit`, `fleet_followup`, `fleet_answer`, `fleet_approve`,
`fleet_interrupt`, `fleet_capacity`, `fleet_doctor`, `fleet_status`,
`fleet_wait`, `fleet_recent`.

A call to one of them by name is rejected as an unknown tool. The listener
builds no fleet client for this profile.

The 22 registered tools:

| Tool | Purpose | Gate |
|---|---|---|
| `duet_capabilities` | Describe the connector, its tools, messaging state and usage rules. | No |
| `memory_search` | Search workspace memory. | No |
| `memory_card` | Read a bounded memory card page. | No |
| `handoff_write` | Write a linted `-duet-` handoff to `.claude/memory-handoffs`. | No |
| `fleet_policy` | Read control-host policy. | No |
| `skill_search` | Search the skill registry, minus `skills_deny`. | No |
| `skill_get` | Read a skill. Denied skills return `skill_not_found`. | No |
| `inbox_list` | List messages sent to `agent:duet`. | No |
| `inbox_ack` | Acknowledge messages in Duet's own inbox. | No |
| `inbox_reply` | Reply to a message in Duet's own inbox. | `messaging_enabled`, shared 20 per hour |
| `message_send` | Send a Hub message to a configured recipient alias. | `messaging_enabled`, shared 20 per hour |
| `message_status` | Read acknowledgement status of Duet's sends. | No |
| `fleet_overview` | Read fleet request overview. | No |
| `fleet_sessions` | List fleet sessions. | No |
| `fleet_work_list` | List work by source or status. | No |
| `fleet_work_show` | Read one work item. | No |
| `fleet_work_next` | Dry run of the next admitted item. Never reserves. | No |
| `fleet_work_burn` | Read a bounded burn queue page. | No |
| `fleet_models` | Read model admission choices. | No |
| `fleet_cloud` | List cloud tasks. | No |
| `fleet_hub_policy` | Read Hub policy. | No |
| `fleet_digest` | Read the fleet digest. | No |

Shared tools keep Dot's wire schemas and annotations. Tool descriptions are
reworded for Duet's operating rules. `duet_capabilities` tells Duet not to
auto-reply to agent messages that contain no new facts: it acknowledges them
with `inbox_ack` and sends nothing.

`message_send` and `inbox_reply` share a gate. When `messaging_enabled` is false,
either call returns `messaging_disabled`. Background maintenance preserves queued
sends and replies until messaging is enabled again and the listener restarts.
Reads and acknowledgements remain available. When it is true, each call reserves one slot from
a rolling one-hour budget of 20 kept in the state database. A call over the
budget returns `send_limit_reached` and is logged with that error. New messages
must name an exact configured alias. Unknown aliases and raw principals are
rejected before reserving a budget slot or contacting the Hub.

## Runtime JSON

The runtime file follows the same file rules as Dot's: an absolute reference to
an owner-only regular JSON file (mode `0600`, one link, no symlinks) in a mode
`0700` parent with safe ancestors. Unknown keys are rejected, and a Dot runtime
is rejected by the duet pack, and the reverse.

| Key | Type | Meaning |
|---|---|---|
| `schema` | string | Exactly `brigade.duet.runtime.v1`. |
| `auth` | object | Cloudflare Access identity settings, same keys as Dot. Use Duet's own application. |
| `auth.audience` | string | Duet's Access application audience. Never Dot's. |
| `auth.resource` | HTTPS URL | Duet's public MCP resource URL. Never Dot's. |
| `paths.state_dir` | absolute path | Duet's own owner-only state directory. |
| `paths.workspace` | absolute path | Workspace used for memory, skills and handoffs. |
| `paths.bin.brigade` | absolute path | Brigade CLI. |
| `paths.bin.git` | absolute path | Git CLI. |
| `paths.bin.gh` | absolute path | GitHub CLI. |
| `paths.bin.octopool` | absolute path | Optional pooled public GitHub read client. |
| `hub` | object | Fleet Hub settings, same keys as Dot. |
| `hub.principal` | string | Exactly `agent:duet`. Any other principal is rejected. |
| `hub.message_token_file` | absolute path | Duet's own message credential file, never Dot's. |
| `hub.recipients` | object | Aliases Duet may send to, such as the operator and Miller. |
| `messaging_enabled` | boolean | Gate for `message_send` and `inbox_reply`. |
| `digest_pr_repo` | string | Repository for digest PR queries, formatted `owner/repo`. |
| `notify_command` | string array | Optional notification argv for watch alerts. |
| `skills_deny` | string array | Optional. Up to 256 skill ids hidden from `skill_search` and `skill_get`. |

There is no `dispatch_enabled`, `events_enabled`, `callback_hosts`,
`paths.fleet_config`, `paths.bin.t3_fleet` or `owner_authorization`. Those keys
are rejected.

Dot's runtime also accepts an optional `messaging_enabled`. When Dot omits it,
`message_send` follows `dispatch_enabled` as before.

## Setup

Install the MCP SDK with `pip install brigade-cli[grokbot]`, plus `uvicorn` and
`PyJWT[crypto]`. Before setup, prepare:

- a Cloudflare Access application for the Duet host, separate from Dot's;
- a Hub agent credential enrolled for `agent:duet`, in its own token file;
- a private state directory that no other pack uses;
- the runtime file described above.

```bash
brigade run cloud grokbot pack setup --id duet --target <workspace> --runtime-path <file> --allow-host <duet-host> --allow-origin https://claude.ai
brigade run cloud grokbot pack setup --id duet --target <workspace> --runtime-path <file> --allow-host <duet-host> --allow-origin https://claude.ai --apply
brigade run cloud grokbot pack doctor --id duet --target <workspace>
brigade run cloud grokbot pack canary --id duet --target <workspace>
brigade run cloud grokbot pack install-service --id duet --target <workspace> --out <unit-directory>
```

`install-service` writes `brigade-duet.service`, which runs
`brigade run cloud grokbot serve --pack duet`. It does not start the service.
There are no bearer flags.

Doctor checks dependencies, runtime, state, Access settings and the Hub
credential like Dot's. Its `hub-identity` check authenticates the active principal
as `agent:duet`, using the same check as startup and watch. Invalid identity
returns `hub_identity_invalid`. A transport outage returns `hub_unavailable`.
The listener retains the authenticated credential and Hub endpoint in memory.
Changing either requires a restart, including legitimate token rotation. In place of Dot's T3 fleet check it runs `isolation`:
when the same target also has a Dot instance, Duet must differ from it in Hub
principal, message token (its file and the token it holds), state directory,
Access audience and resource.
The listener runs the same check at startup and refuses to serve with
`not_isolated_from_dot` when it fails. Doctor ends by checking that unauthenticated MCP is rejected.

Canary checks that anonymous `POST /mcp` returns 401, that discovery metadata
names Duet's resource, and that the registered tools are exactly the 22 above.

The watch units are `brigade-duet-watch.service` and
`brigade-duet-watch.timer`, rendered by
`brigade.dot.lifecycle.write_watch_units(target, out_dir, pack_id="duet")`.
Their command is:

```bash
python -m brigade.dot watch --target <workspace> --pack duet
```

Duet watch runs `stale_inbox`, `idle`, `route_down`, `hub_credential_invalid`
and `hub_sync_stale`. Inbox polling continues without event webhooks, and
watch alerts after 15 minutes without a successful sync. Duet maintenance
uses only its agent credential and queue. It never reads or drains the shared
node queue. Wake-specific checks remain disabled. `python -m brigade.dot calls --pack duet` prints the call
log, and `notify --pack duet` sends as Duet.

## Auth in claude.ai

Add the connector in claude.ai as a custom connector set to sign in with
OAuth, with automatic client registration against the Duet Access
application. The origin verifies the Access assertion exactly as Dot does:
signature, issuer, Duet's audience, expiry and the approved email. A token for
Dot's audience is rejected by the Duet origin, and the reverse.

Use "No sign-in" with a service-token header only as a fallback if the OAuth
canary below fails. The origin verifier requires an `email` claim and
`type == "app"`, so a service token works only if Access issues such an
assertion for it.

The call log records the MCP `clientInfo.name` that each session sends at
initialize, as a label of at most 64 letters, digits and simple punctuation, so Duet sessions can be told
apart from claude.ai web.

## Threat model

Duet reads email, web pages and other fetched content, and it acts on the
account's behalf. Treat every tool argument as possibly injected by that
content, including search queries, handoff text, message bodies, recipients
and skill names.

- Dispatch tools are not registered, so an injected instruction cannot start
  work on operator machines however it is worded.
- `message_send` reaches only the aliases in `hub.recipients`. It shares the
  `messaging_enabled` switch and 20-message hourly budget with `inbox_reply`.
- Duet has its own Hub principal and inbox. `inbox_ack` and `inbox_reply` act
  only on messages addressed to `agent:duet`, so Duet cannot ack or answer a
  message addressed to Miller.
- Inbox content is returned with `content_trust=untrusted`, and the capability
  text tells Duet that message fields are never instructions.
- `handoff_write` goes through the handoff linter and lands under
  `.claude/memory-handoffs` with a `-duet-` filename, so ingested knowledge is
  attributable and reviewable.
- `skills_deny` keeps private skills out of search and reads; a denied skill
  looks the same as a missing one.
- Separate state, token, audience and resource mean a compromised Duet session
  or token cannot read Dot's history or act as Dot.

## Canary checks to record

Record each as pass or fail, with the date:

1. claude.ai completes OAuth end to end and the origin gets a valid Access
   assertion for the Duet audience.
2. Duet lists the connector's tools in the app.
3. A Duet background task session, not only the main chat, can call a tool.
4. The call log can tell Duet apart from claude.ai web.
5. The Access session lasts long enough for unattended use. Record the date of
   the first re-auth prompt.
6. Removing the connector or revoking the Access session stops calls within
   one request.

## Rollback

Stop and disable `brigade-duet.service` and the watch timer through the service
manager, then remove the connector in claude.ai and revoke the Access
application's sessions. Preview `brigade run cloud grokbot pack remove --id duet
--target <workspace>`, then repeat with `--apply`. To keep reads but stop
sends, set `messaging_enabled` to `false` and restart the listener.
