# Dot connector pack

The dot pack connects a ChatGPT dot to T3 fleet dispatch, Brigade memory,
and the Fleet Hub inbox. It exposes 32 closed tool contracts. Its default bind
is `127.0.0.1:8779`. Its registry `public_route` is empty. Configure a dedicated
public host and route `/mcp` through Cloudflare Access to this listener.

## Platform support

The local listener, runtime-file loading, SQLite state, watcher, notification
and call-log commands require Linux. The bridge keeps its existing `flock`
locking and opens SQLite through `/proc/self/fd` to pin the private directory.
These operations refuse Windows with `unsupported_on_windows` before opening
local configuration or creating state. This preserves the non-POSIX refusal
introduced in #1765. Brigade's other commands remain available on Windows.
Imports, runtime-document parsing, authentication and HTTP protocol validation
are portable. Dot's bounded command runner uses Brigade's owned job objects
on Windows.

The shared NT private-file helpers do not provide a descriptor-pinned SQLite
connection. Windows state support needs that storage contract before the
listener can safely run there.

## Runtime JSON

The instance stores `runtime_path`, an absolute reference to an owner-only
regular JSON file with exactly one link. Use mode `0600`, no symlinks,
and a mode `0700` parent directory with safe ancestors. Paths must already be
absolute. Variables are not expanded.
Unknown keys are rejected. All fields below are required except those marked
optional. Replace placeholders with the deployment's configured values.

| Key | Type | Meaning |
|---|---|---|
| `schema` | string | Exactly `brigade.dot.runtime.v1`. |
| `auth` | object | Cloudflare Access identity settings. |
| `auth.issuer` | HTTPS URL | Exactly `https://` followed by `auth.team_domain`. |
| `auth.team_domain` | hostname | Cloudflare Access team host used to fetch signing keys. |
| `auth.audience` | string | Application audience, 64 lowercase hexadecimal characters. |
| `auth.approved_email` | string | Only identity admitted by the origin. |
| `auth.resource` | HTTPS URL | Public MCP resource URL advertised in discovery metadata. |
| `paths` | object | Local storage and executable references. |
| `paths.state_dir` | absolute path | Owner-only SQLite and call-log directory. |
| `paths.workspace` | absolute path | Workspace used for memory and handoffs. |
| `paths.fleet_config` | absolute path | Fleet client configuration. |
| `paths.bin` | object | Executable paths. |
| `paths.bin.t3_fleet` | absolute path | T3 fleet client. |
| `paths.bin.brigade` | absolute path | Brigade CLI. |
| `paths.bin.git` | absolute path | Git CLI. |
| `paths.bin.gh` | absolute path | GitHub CLI. |
| `paths.bin.octopool` | absolute path | Optional pooled public GitHub read client. |
| `hub` | object | Fleet Hub inbox settings. |
| `hub.principal` | string | Agent principal, such as `agent:example`. |
| `hub.node_id` | UUID string | Enrolled node identity. |
| `hub.message_token_file` | absolute path | Protected message credential file reference, never the token. |
| `hub.poll_seconds` | integer | Hub poll interval, 10 through 300 seconds. |
| `hub.recipients` | object | Up to 16 aliases mapped to agent principals or node UUIDs. |
| `dispatch_enabled` | boolean | Gate for the six dispatch/message tools marked below. |
| `events_enabled` | boolean | Enable event capabilities, RPCs, workers, fleet polling, and webhooks. When false, pass requests straight to MCP. |
| `digest_pr_repo` | string | Repository for digest PR queries, formatted `owner/repo`. |
| `callback_hosts` | string array | Nonempty allowlist of callback hostnames. |
| `notify_command` | string array | Optional notification argv. The first item is an absolute executable path. |
| `owner_authorization` | object | Owner's recorded automation authorization. |
| `owner_authorization.version` | string | Nonempty printable ASCII authorization version, at most 64 characters. |
| `owner_authorization.text` | string | Nonempty authorization text, at most 4096 UTF-8 bytes. |

## Setup and service units

Install the MCP SDK with `pip install brigade-cli[grokbot]`. The listener also
requires `uvicorn` and `PyJWT[crypto]` in its Python environment.
Prepare the runtime, enrolled Hub credential, and private state directory first.
Use `--id dot` for pack commands, matching the other registered packs.

```bash
brigade run cloud grokbot pack setup --id dot --target <workspace> --runtime-path <file> --allow-host <public-host> --allow-origin <public-origin>
brigade run cloud grokbot pack setup --id dot --target <workspace> --runtime-path <file> --allow-host <public-host> --allow-origin <public-origin> --apply
brigade run cloud grokbot pack doctor --id dot --target <workspace>
brigade run cloud grokbot pack canary --id dot --target <workspace>
brigade run cloud grokbot pack install-service --id dot --target <workspace>
brigade run cloud grokbot pack install-service --id dot --target <workspace> --out <unit-directory>
```

The last command writes `brigade-dot.service`. It does not start the service.
There are no bearer flags. Doctor checks dependencies, runtime, state, Access
settings, Hub credential, fleet capacity, and rejection of unauthenticated MCP.
Canary checks unauthenticated rejection, discovery metadata, and tool inventory.

The watch service and timer are rendered separately by
`brigade.dot.lifecycle.write_watch_units(target, out_dir)`. They are
`brigade-dot-watch.service` and `brigade-dot-watch.timer`. The timer runs
five minutes after boot and every five minutes thereafter, with 30-second
accuracy. Its command is:

```bash
python -m brigade.dot watch --target <workspace>
```

Review the generated units, then install and enable them through the deployment's
service manager. Unit rendering and pack setup never start, stop, or reload a
service. The listener and watcher use `NoNewPrivileges=yes`, `UMask=0077`, and
`PrivateTmp=no`, relying on the private-directory ownership walk for filesystem
safety. Namespace sandboxing is omitted because it maps root-owned ancestors to
overflow uid 65534 in user units, which the ownership walk intentionally rejects.

## Tools

Gated tools require `dispatch_enabled: true`. Other mutations, including inbox
acknowledgments, replies, and handoff writes, retain their own validation rules.

| Tool | Purpose | Dispatch gate |
|---|---|---|
| `dot_t3_capabilities` | Describe capabilities and gates. | No |
| `fleet_capacity` | Read host capacity. | No |
| `fleet_doctor` | Diagnose host or repository dispatch readiness. | No |
| `fleet_submit` | Submit a fleet task. | Yes |
| `fleet_status` | Read request state and optional output. | No |
| `fleet_wait` | Wait up to 60 seconds for a request. | No |
| `fleet_followup` | Send a follow-up prompt. | Yes |
| `fleet_answer` | Answer a pending input request. | Yes |
| `fleet_approve` | Respond to an approval request. | Yes |
| `fleet_interrupt` | Interrupt a request. | Yes |
| `fleet_recent` | List recent requests. | No |
| `fleet_overview` | Read fleet request overview. | No |
| `memory_search` | Search workspace memory. | No |
| `memory_card` | Read a bounded memory card page. | No |
| `handoff_write` | Write a validated memory handoff. | No |
| `inbox_list` | List inbox messages. | No |
| `inbox_ack` | Acknowledge selected messages. | No |
| `message_send` | Send an authorized Hub message. | Yes |
| `message_status` | Read delivery status. | No |
| `fleet_policy` | Read fleet policy changes. | No |
| `skill_search` | Search the skill registry. | No |
| `skill_get` | Read a skill or supporting resource. | No |
| `inbox_reply` | Reply to an inbox message. | No |
| `fleet_sessions` | List fleet sessions. | No |
| `fleet_work_list` | List work by source or status. | No |
| `fleet_work_show` | Read one work item. | No |
| `fleet_work_next` | Read the next admitted work item. | No |
| `fleet_work_burn` | Read a bounded burn queue page. | No |
| `fleet_models` | Read model admission choices. | No |
| `fleet_cloud` | List cloud tasks. | No |
| `fleet_hub_policy` | Read Hub policy. | No |
| `fleet_digest` | Read the fleet digest. | No |

## Security model

Dot verifies the Cloudflare Access JWT at the origin, including the signature,
issuer, audience, expiry, and approved email. It has no static origin bearer.
Signing keys come from the configured team host over verified TLS with bounded
responses and refreshes. Host and Origin allowlists apply before authentication.
Host entries match the full header case-insensitively, including its port. A loopback bind also
accepts exactly `127.0.0.1:<bind-port>` and `localhost:<bind-port>`.
Only `POST /mcp` and `GET /.well-known/oauth-protected-resource/mcp` are admitted.
Queries and alternate routes are rejected. Discovery metadata is public, while
MCP requests require the approved Access identity.

Keep the runtime file and state directory owned by the listener user with no
group or other access. Store credentials in protected files referenced by the
runtime. Tool schemas reject unknown arguments, and subprocess calls use fixed
argv with bounded input and output. The dispatch gate defaults to the runtime's
explicit boolean and does not replace fleet admission or owner authorization.

## Rollback

Disable and stop the listener and watch timer through the service manager before
removing their units. Preview `brigade run cloud grokbot pack remove --id dot
--target <workspace>`, then repeat with `--apply` when ready to remove the owned
instance config. Remove the separately installed watch units explicitly. Preserve
the private runtime and state for recovery, or remove them under the owner's
retention policy. To suspend dispatch while keeping reads available, set
`dispatch_enabled` to `false` and restart the listener with the updated runtime.
