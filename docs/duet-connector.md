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

Duet defaults to its own cloud compute. Without dispatch keys, the listener
builds no fleet client and registers the same 22 tools below. Dispatch is an
explicit owner opt-in. With dispatch enabled it adds exactly seven tools:

| Tool | Purpose |
|---|---|
| `fleet_submit` | Start one bounded task in an isolated fleet worktree. |
| `fleet_status` | Read a Duet-owned request's state. |
| `fleet_wait` | Wait at most 60 seconds for a Duet-owned request. |
| `fleet_recent` | Read Duet's local operation journal. |
| `fleet_capacity` | Inspect fleet capacity and repository aliases. |
| `fleet_followup` | Start a bounded additional turn on a Duet-owned request. |
| `fleet_interrupt` | Interrupt a Duet-owned request. |

`fleet_approve`, `fleet_answer`, `fleet_doctor` and `fleet_report_session`
are always absent. Calls to absent tools are rejected as unknown tools.
`duet_capabilities` reports the active tool inventory, dispatch state and limits.

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

`memory_search` forwards each match's `score` from `brigade memory search --json`. Since #2196 it is a
BM25 relevance score: a non-negative float rounded to 4 decimals, where higher ranks first. It used to
be a small integer. Scores compare only within one result list, never across workspaces or queries.
Equal scores are ordered by card path.

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
| `memory_push` | boolean | Optional, default `true`. `false` stops `inbox_list` and `fleet_digest` from carrying `memory_hits` (see the Dot guide). |

`events_enabled` and `callback_hosts` remain rejected. To enable dispatch,
add these fields to the runtime JSON, adding the path members to the existing
`paths` and `paths.bin` objects:

```json
{
  "dispatch_enabled": true,
  "owner_authorization": {
    "version": "1",
    "text": "Owner-authorized bounded work on the configured repositories."
  },
  "dispatch_limits": {
    "repositories": ["example-project"],
    "max_open": 3,
    "daily_limit": 20,
    "prompt_max_bytes": 32768
  },
  "paths": {
    "fleet_config": "/srv/duet/fleet.toml",
    "bin": {"t3_fleet": "/usr/local/bin/t3-fleet"}
  }
}
```

Merge this fragment into the complete runtime. Dispatch requires
both paths and owner authorization. Use a dedicated fleet config and client
identity for Duet, never Dot's config or credentials. Fleet submits are labeled
`duet`. The startup isolation check also rejects a fleet config shared with Dot.
Omit all dispatch fields to retain the default. `dispatch_enabled: false` is
also accepted without dispatch paths, authorization or limits.

`dispatch_limits` is optional when enabled. Defaults are 3 open requests,
20 attempts per UTC day and 32768 UTF-8 bytes per prompt. `repositories` is an
optional list of fleet repository aliases. Omission allows configured aliases,
and an empty list denies all. `max_open` accepts 1 through 20, `daily_limit`
1 through 200, and `prompt_max_bytes` 1 through 32768. Prompt bounds include
appended owner authorization and apply to followups too, including read-only
calls. Read-only calls still count toward admission limits.

Submit and followup admission reserve slots and daily attempts atomically in
Duet's private SQLite state before fleet IO. Followups on a finished request
must reacquire an open slot. Definite fleet refusals release the slot but consume
the attempt. Unknown outcomes or a crash retain the reservation: inspect
`fleet_recent` and ask the operator to reconcile before retrying. Status and
wait release slots only after a confirmed terminal state. Interrupt alone does
not release a slot. No automatic expiry can reopen capacity for unknown work.
Only requests created through this Duet state can be read or controlled.
Typed refusals include `repository_not_allowed`, `open_limit`, `daily_limit`,
`prompt_too_large`, `unknown_request` and `request_busy`.

Dot's runtime also accepts an optional `messaging_enabled`. When Dot omits it,
`message_send` follows `dispatch_enabled` as before.

## Setup

Install the MCP SDK with `pip install brigade-cli[grokbot]`, plus `uvicorn` and
`PyJWT[crypto]`. Before setup, prepare:

- a Cloudflare Access application for the Duet host, separate from Dot's.
- a Hub agent credential enrolled for `agent:duet`, in its own token file.
- a private state directory that no other pack uses.
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
names Duet's resource, and that the registered tools match the runtime: 22 by default, 29 with dispatch.

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

- Dispatch tools are absent by default. Opting in permits injected content to
  attempt bounded work on operator machines. Repository, open-request, daily
  attempt and prompt limits reduce exposure. They do not establish prompt trust.
  Approval, answer and doctor tools remain absent in every configuration.
  Do not enable dispatch for content whose authority you cannot evaluate.
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
- `skills_deny` keeps private skills out of search and reads. a denied skill
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

To disable dispatch while keeping reads and messaging, remove `dispatch_enabled`,
`owner_authorization`, `dispatch_limits`, `paths.fleet_config` and
`paths.bin.t3_fleet`, then restart the listener. This removes all seven dispatch
tools and builds no fleet client. Running tasks continue. the operator must
interrupt them separately if needed. Retain Duet's SQLite state for audit and
admission accounting if dispatch is enabled again.
