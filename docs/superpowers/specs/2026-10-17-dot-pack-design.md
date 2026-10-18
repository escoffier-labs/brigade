# Dot connector pack design

Date: 2026-10-17. Status: proposed.

## Goal

Move the Dot bridge into Brigade as a connector pack named `dot`. Dot is the ChatGPT "dot" planner that reaches the fleet through a remote MCP server. The bridge runs today as loose files in a private directory outside any repository. After the move it gets version control, CI, `pack doctor`, `pack canary`, Brigade-rendered systemd units and a pipx install. ChatGPT must see no change: same public hostname, same login, same tool names and schemas.

## Non-goals

- No new public endpoint, tunnel or DNS record.
- No change to the hub message protocol or to t3-fleet.
- No generalization of the `grokbot` pack registry naming. The pack registers there because that is where doctor, canary and unit rendering already live. Renaming the registry is separate work.
- No refactor of `brigade.grokbot_t3`. The Dot pack imports its generic pieces where they fit. Moving them into a shared module is a later, separate change coordinated with the t3-dispatch owner.
- Outside health checks of the public endpoints (plan phase 5) get their own spec.

## Decisions already settled

These come from the fleet plan and need no re-approval:

- Pack id `dot`, in this repository, no new repository.
- Dot keeps its Cloudflare Access email login. ChatGPT cannot send an Access service token, so the origin validates the Access JWT itself.
- Tool names and input schemas stay identical.
- Cut over only after parity tests pass. The old bridge directory is retired with a tarball backup.

## Decisions made in this design

1. **Drop the three legacy review tools** (`dot_t3_submit_review`, `dot_t3_job_status`, `dot_t3_job_result`). The bridge's own call log shows no call to any of them in the last 30 days. They drive a private sender script that lives outside any repository and pin a project id and commit. `dot_t3_capabilities` stays, because Dot reads it as its usage guide. its text is rewritten to stop advertising the dropped tools.
2. **Drop the local inbox backend.** The bridge runs in hub mode. The local sqlite inbox and the one-shot `dot-hub-migrate` script exist only for the migration that already ran.
3. **Use the hub message Python API** (`brigade.fleet_client_messages`) instead of shelling out to `brigade fleet msg`. Same calls, same token file, no argv exposure of message bodies.
4. **Use `brigade.grokbot_t3.fleet.FleetRunner` for every t3-fleet call.** It sends the request as JSON on stdin, so prompts and titles never reach argv. The bridge currently builds argv flags. The initiator label stays `agent`/`dot`.
5. **Keep the state schema.** The pack opens the existing state directory and its two databases unchanged, so ChatGPT's event subscriptions, tracked requests, the operations journal and the call log survive the cutover. Any schema change in this first version is additive, so the old bridge can still start on the same files for rollback.
6. **One added optional dependency.** RS256 verification needs `cryptography`, which `mcp>=2` does not pull in (it depends on PyJWT without the `crypto` extra). The `grokbot` extra now lists `pyjwt[crypto]>=2` (owner-approved 2026-10-17). Core Brigade stays zero-dependency. The verifier imports PyJWT lazily, `doctor` checks both PyJWT and `cryptography`, and a missing RSA backend fails closed with `OptionalDependencyError`.

## Architecture

New package `src/brigade/dot/`. Every module stays well under the 2000-line ceiling. the targets below are budgets, not estimates to grow into.

| Module | Job | Budget |
|---|---|---|
| `contracts.py` | Pack id, default bind, tool names, input schemas, limits, error codes | 450 |
| `runtime_config.py` | Owner-only runtime JSON: auth, hosts, paths, hub, flags, recipients, owner authorization text | 300 |
| `access_auth.py` | Cloudflare Access JWT verifier: RS256 only, JWKS fetch with 300 s cache, issuer, audience, exp/nbf/iat, `type == "app"`, exact email match. Every identity value comes from runtime config | 300 |
| `safe_state.py` | Private directory walk, private file checks, flock-guarded sqlite opened through the directory fd | 250 |
| `state.py` | Hub inbox cache, replies, operations journal, call log with 30-day retention | 450 |
| `events.py` | MCP `events/*` RPC: subscribe, unsubscribe, list, and the hub poller | 450 |
| `event_app.py` | Optional ASGI event transport, capability injection, and worker lifecycle | 150 |
| `webhooks.py` | Standard Webhooks signing and bounded delivery to the allowed callback host | 350 |
| `fleet_tools.py` | `fleet_*` t3-fleet tools over `FleetRunner`, source revision resolution, dispatch gate | 500 |
| `hub_tools.py` | Hub reads (`fleet_sessions`, `fleet_work_*`, `fleet_models`, `fleet_cloud`, `fleet_hub_policy`), `fleet_overview`, `fleet_digest` | 550 |
| `memory_tools.py` | `memory_search`, `memory_card`, `handoff_write`, `fleet_policy`, `skill_get`, `skill_search` | 350 |
| `inbox_tools.py` | `inbox_list`, `inbox_ack`, `inbox_reply`, `message_send`, `message_status` | 300 |
| `lifecycle.py` | Request gate, ASGI app, `build_app`, `run_listener`, `doctor`, `canary`, unit rendering | 550 |
| `watch.py` | The stale-inbox watch: stale message, idle Dot, invalid hub credential, route down, unanswered wake | 350 |
| `__main__.py` | `python -m brigade.dot serve`, `notify`, `calls`, `watch` | 120 |

`notify` replaces `dot-notify`, `calls` replaces `bridge-calls`, and `watch` replaces `dot-inbox-watch`.

### Request path

1. cloudflared forwards the public hostname to the pack's loopback bind.
2. The gate admits only `GET /.well-known/oauth-protected-resource/mcp` and `POST /mcp`. anything else is 404.
3. The gate checks Host and Origin against the instance allowlists, then verifies the `Cf-Access-Jwt-Assertion` header. A failure returns 401 with a `www-authenticate` header naming the resource metadata, as today.
4. The MCP SDK (`mcp.server.MCPServer`, streamable HTTP, stateless) dispatches the tool. Gated tools raise `dispatch_disabled` unless the runtime config enables dispatch.
5. Each call is recorded in the call log with a bounded argument summary and no prompt text.

### Configuration

The pack instance file (`.brigade/grokbot/packs/dot.json`) holds `bind`, `allowed_hosts`, `allowed_origins`, `public_route` and `runtime_path`, like the t3-dispatch pack. It has no `bearer` reference: Dot authenticates with the Access JWT instead. The runtime JSON at `runtime_path` (mode 0600, owner-checked, no symlinks) holds:

- `auth`: `issuer`, `team_domain`, `audience`, `approved_email`, `resource`.
- `paths`: `state_dir`, `workspace`, `fleet_config`, plus optional binary overrides for `t3-fleet`, `brigade`, `git`, `gh` and `octopool`.
- `hub`: `principal`, `node_id`, `message_token_file`, `poll_seconds` (10 to 300), `recipients` (alias to node id or `agent:` principal, at most 16).
- The 2026-10-17 cutover found that user-namespace sandboxing maps ancestor owners to overflow uid 65534, so both units use `NoNewPrivileges=yes`, `UMask=0077`, `PrivateTmp=no` and the fail-closed private-directory walk, with no `writable_paths` runtime key.
- `dispatch_enabled`, `events_enabled`, `digest_pr_repo`, `callback_hosts`, `notify_command` (for `watch` alerts), `owner_authorization` (`version`, `text`).

The issuer must equal `https://` plus `auth.team_domain`. Runtime loading requires a single-link mode-0600 file under a mode-0700 parent reached through safe ancestors. Owner authorization text is limited to 4096 UTF-8 bytes and its version to 64 printable ASCII characters.

When `events_enabled` is false, the event transport is a pure MCP passthrough with no event capabilities, RPC interception or workers.

Every host name, path, email, port, node id and organization name in the bridge moves into this file. Nothing private is tracked in the repository.

### Registry changes (`brigade.grokbot_packs`)

Add `dot` to `CONNECTOR_DEFAULT_BINDS` with default bind `127.0.0.1:8779`, plus its tools, instance keys, runtime path validator, and the `doctor`, `canary`, `render_install_service` and `serve --pack` branches. `grokbot_packs.py` is at 1595 lines, so these additions stay small and delegate to `brigade.dot.lifecycle`.

`canary` is special for this pack. The gate admits only two routes, and `tools/list` needs an Access JWT, which a canary cannot mint. So the canary checks that an anonymous `POST /mcp` gets 401, that the resource metadata document answers, and that the pack's in-process tool registry matches the frozen tool snapshot.

## Failure handling

- Missing `mcp` or PyJWT: `doctor` reports the dependency check failed and `serve` exits with `OptionalDependencyError`, as other packs do.
- JWKS fetch failure: 401 for that request, logged without the token. The cached key set keeps serving until its 300 s expiry.
- Unsafe state directory, database or runtime file: `serve` refuses to start and `doctor` names the failing check.
- Hub unreachable: inbox tools keep today's behavior and serve the hub cache. `send` and `reply` spool through the client's durable offline queue.
- t3-fleet errors pass through as `fleet_error` results containing the complete `fleet` error object and `exit_code`, plus `request_id` when present. The operation journal keeps the redacted message, truncated to 300 characters.
- Hub refusal codes retain the bridge's fixed retry guidance. Unexpected invoke exceptions return `adapter_unavailable`.

## Testing

- Port the bridge's 204 tests into `tests/test_dot_*.py`, one file per module. Drop the tests for the removed legacy tools and local inbox. Every temporary directory is created under the test's own private parent, so a world-writable TMPDIR cannot fail the private-directory check.
- `tests/test_dot_contracts.py` freezes the tool list and every input schema in a JSON snapshot, captured from the running bridge before the port. A schema drift fails CI.
- Access JWT tests sign tokens with a generated RSA key and serve it through a fake JWKS: wrong issuer, wrong audience, wrong email, expired, not-yet-valid, `type` not `app`, non-RS256 algorithm, unknown key id.
- A gate test proves only the two admitted routes answer and Host/Origin rejections return before any tool code runs.
- `test_grokbot_packs.py` gains the `dot` registry cases.
- CI runs the suite with the `grokbot` extra, like the t3-dispatch pack.

## Cutover

1. Capture the frozen tool snapshot from the running bridge.
2. Install Brigade with the `grokbot` extra into the main pipx venv.
3. Convert the bridge config into the runtime JSON and run `pack setup` and `pack doctor`.
4. Start the pack on its default port 8779 against a copy of the state directory. Run `pack canary` and compare `tools/list` (fetched in-process) with the snapshot.
5. Stop the old bridge service. Render the pack unit with bind `127.0.0.1:8787`, so the tunnel's origin is unchanged, pointed at the real state directory. Start it.
6. Check the public hostname answers 401 anonymously. Send Dot a hub message asking it to call `dot_t3_capabilities` and `fleet_capacity`, and confirm both calls in the call log.
7. Repoint the stale-inbox watch timer at `python -m brigade.dot watch`.
8. After a week with no rollback, tar the old directory into the backup area and remove it.

Rollback: stop the pack unit and start the old bridge service. The state files are compatible because the first version makes no destructive schema change.

## Risks

- ChatGPT caches tool schemas per connector. Any schema difference could force Dot to re-register the connector. The frozen snapshot test guards it.
- Event subscriptions live in the state database. Pointing the pack at a fresh directory would silently stop wakes. The cutover uses the existing directory.
- The pack registry lives under `brigade run cloud grokbot pack`, which reads oddly for a ChatGPT client. That is cosmetic and left to the registry rename.
- `grokbot_t3` has an open PR (#1746). The Dot pack only imports `FleetRunner`, so the two do not conflict.
