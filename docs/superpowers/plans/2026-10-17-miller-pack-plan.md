# Miller connector pack implementation plan

Spec: `docs/superpowers/specs/2026-10-17-miller-pack-design.md` (approved 2026-10-17).

**Goal:** port the Miller ChatGPT MCP bridge into `src/brigade/miller/` as the `miller` connector pack, with identical tool names and schemas, an origin-side Cloudflare Access JWT gate, and state carried over unchanged.

**Architecture:** one package of small modules (budgets in the spec), wired into the existing connector-pack registry in `src/brigade/grokbot_packs.py` and the `serve --pack` branch of `src/brigade/cli/run_cloud.py`. t3-fleet calls go through `brigade.grokbot_t3.fleet.FleetRunner.call` (JSON on stdin). Hub messaging goes through `brigade.fleet_client_messages`. Serving uses the `mcp>=2` SDK (`mcp.server.MCPServer`, streamable HTTP, stateless) under uvicorn, exactly like `src/brigade/grokbot_t3/lifecycle.py`.

**Tech:** Python 3.11+, stdlib plus the `grokbot` extra (`mcp>=2,<3`, which brings PyJWT, starlette, uvicorn). ruff (line length 120), mypy, pytest.

**For agentic workers:** execute task by task, ticking the checkboxes. Leave changes uncommitted only if your dispatch says so; otherwise commit at each task's last step. Run every check through `brigade work verify run --target . --command "<check>"`. Never commit anything from the bridge's private config, state or token files.

## Inputs every worker needs

- `$BRIDGE`: the bridge source directory. The dispatcher passes the real path; it is never written into the repository. Line ranges below refer to the bridge as of 2026-10-17 (`dot_mcp.py` is 2647 lines, `dot_events.py` 775, `dot_auth.py` 308, `dot_hub.py` 336, `dot_fleet.py` 144, `dot-inbox-watch` 323, `bridge-calls` 117, `dot-notify` 122).
- The bridge's tests are in `$BRIDGE/tests/` (204 tests).
- Content guard: no home paths, private host names, emails, node ids or tokens in any tracked file. Tests use `example.test` hosts, `miller@example.test`, and `00000000-0000-4000-8000-00000000000N` style ids. Check with `brigade guard scan <file> --policy src/brigade/guard/policies/public-repo.json`.

## File map

| Path | Responsibility |
|---|---|
| `src/brigade/miller/__init__.py` | Exports `PACK_ID`, `TOOLS`, `MillerError`, `doctor`, `canary`, `render_unit`, `write_unit`, `write_watch_units`, `validate_runtime_reference`, `build_listener_from_target`, `run_listener` |
| `src/brigade/miller/contracts.py` | Pack constants, tool names, input schemas, limits, error type |
| `src/brigade/miller/runtime_config.py` | Owner-only runtime JSON parsing into a frozen `Runtime` |
| `src/brigade/miller/access_auth.py` | Cloudflare Access JWT verifier and JWKS cache |
| `src/brigade/miller/safe_state.py` | Private directory walk, private file check, `PrivateDatabase` |
| `src/brigade/miller/process.py` | `CommandResult`, `CommandRunner`, `child_environment`, result bounding (`bound_result`, `parse_json_bytes`) |
| `src/brigade/miller/state.py` | `BridgeState`: hub inbox cache, replies, operations journal, call log |
| `src/brigade/miller/events.py` | `EventService`: `events/*` RPC, tracked requests, hub poller |
| `src/brigade/miller/webhooks.py` | Standard Webhooks signing and bounded HTTPS delivery |
| `src/brigade/miller/fleet_tools.py` | `fleet_capacity`, `fleet_doctor`, `fleet_submit`, `fleet_status`, `fleet_wait`, `fleet_recent`, `fleet_followup`, `fleet_answer`, `fleet_approve`, `fleet_interrupt` |
| `src/brigade/miller/hub_tools.py` | `fleet_overview`, `fleet_sessions`, `fleet_work_list`, `fleet_work_show`, `fleet_work_next`, `fleet_work_burn`, `fleet_models`, `fleet_cloud`, `fleet_hub_policy`, `fleet_digest` |
| `src/brigade/miller/memory_tools.py` | `memory_search`, `memory_card`, `handoff_write`, `fleet_policy`, `skill_get`, `skill_search` |
| `src/brigade/miller/inbox_tools.py` | `inbox_list`, `inbox_ack`, `inbox_reply`, `message_send`, `message_status` |
| `src/brigade/miller/adapter.py` | `MillerTools`: composes the four tool modules, the dispatch gate, the call log, and `dot_t3_capabilities` |
| `src/brigade/miller/lifecycle.py` | Gate, ASGI app, `build_app`, `run_listener`, target loading, `doctor`, `canary`, unit rendering |
| `src/brigade/miller/watch.py` | Stale-inbox watch checks and alert dispatch |
| `src/brigade/miller/__main__.py` | `serve`, `notify`, `calls`, `watch` entry points |
| `src/brigade/grokbot_packs.py` | Register `miller` (modify) |
| `src/brigade/cli/run_cloud.py` | `serve --pack miller` branch (modify) |
| `tests/fixtures/miller_tools_snapshot.json` | Frozen tool names, descriptions and input schemas |
| `tests/test_miller_contracts.py`, `test_miller_runtime.py`, `test_miller_access_auth.py`, `test_miller_state.py`, `test_miller_events.py`, `test_miller_fleet_tools.py`, `test_miller_hub_tools.py`, `test_miller_memory_tools.py`, `test_miller_inbox_tools.py`, `test_miller_lifecycle.py`, `test_miller_watch.py` | Tests per module |
| `tests/test_grokbot_packs.py` | `miller` registry cases (modify) |
| `docs/grokbot-miller.md` | Pack guide, mirroring `docs/grokbot-t3-dispatch.md` |

`adapter.py` exists because the spec's four tool modules need one place for the dispatch gate and call logging; it holds no tool logic (budget 250 lines).

## Pinned decisions

- Pack id `miller`; unit `brigade-miller.service`; default bind `127.0.0.1:8779`; MCP server name stays `dot-t3` and version `"0.28.0"` (the bridge's `MCPServer("dot-t3", version="0.28.0")`), so ChatGPT sees the same server identity.
- Kept tools (32): every tool in `$BRIDGE/dot_mcp.py` `TOOL_SCHEMAS` (lines 875-948) except `dot_t3_submit_review`, `dot_t3_job_status`, `dot_t3_job_result`.
- Errors: `MillerError(code)` with the bridge's existing codes; the tool result shape is unchanged (`{"error": code, ...}` dicts, as the bridge returns today).
- Instance config keys: `MILLER_INSTANCE_KEYS = (INSTANCE_KEYS - {"bearer"}) | {"runtime_path"}`. Miller has no bearer reference.
- Runtime schema id `brigade.miller.runtime.v1`; max size 65,536 bytes; read through `brigade.grokbot_t3.runtime_config.read_owner_file`.
- Unit names: `brigade-miller.service` (listener), `brigade-miller-watch.service` and `brigade-miller-watch.timer` (every 5 minutes, `OnBootSec=5min`, `AccuracySec=30s`).
- The unit runs with `ProtectHome=read-only` plus `ReadWritePaths=` the state directory and the workspace handoff directory, `NoNewPrivileges=yes`, `UMask=0077`. `PrivateTmp=yes`.
- Every temporary directory in tests comes from a `miller_private_dir` fixture. Each test module defines it locally, exactly as below, because the repository keeps pack fixtures per test file.

```python
@pytest.fixture
def miller_private_dir(tmp_path: Path) -> Path:
    parent = tmp_path / "private-parent"
    parent.mkdir(mode=0o700)
    tmp_path.chmod(0o700)
    child = parent / "state"
    return child
```

## Task 1: freeze the tool snapshot

ChatGPT sees the schemas the MCP SDK derives from each registered tool function, not the bridge's `TOOL_SCHEMAS` table. So the snapshot is taken from the bridge's own server registration.

**Files:** Create `tests/fixtures/miller_tools_snapshot.json`. The capture script `scripts/capture_miller_snapshot.py` stays local and untracked: `/scripts/*` is ignored unless allowlisted, and the script only works against the private bridge, which is retired after cutover.

- [x] Write `scripts/capture_miller_snapshot.py`:

```python
"""Capture Miller's registered tools (name, description, input schema, annotations) from the bridge source.

Usage: python scripts/capture_miller_snapshot.py "$BRIDGE" tests/fixtures/miller_tools_snapshot.json
Run with an interpreter that has mcp>=2 installed.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

DROPPED = {"dot_t3_submit_review", "dot_t3_job_status", "dot_t3_job_result"}


def main(bridge: str, out: str) -> int:
    sys.path.insert(0, bridge)
    from mcp.server import MCPServer

    captured: list[MCPServer] = []
    original = MCPServer.streamable_http_app

    def capture(self: MCPServer, *args: object, **kwargs: object) -> object:
        captured.append(self)
        return original(self, *args, **kwargs)

    MCPServer.streamable_http_app = capture  # type: ignore[method-assign]
    import dot_mcp  # type: ignore[import-not-found]

    dot_mcp.build_app(SimpleNamespace(), verifier=SimpleNamespace(validated=True), approved_identity="snapshot@example.test")
    tools = asyncio.run(captured[0].list_tools())
    entries = {
        tool.name: tool.model_dump(mode="json", by_alias=True, exclude_none=True)
        for tool in sorted(tools, key=lambda item: item.name)
        if tool.name not in DROPPED
    }
    Path(out).write_text(json.dumps({"server": "dot-t3", "version": "0.28.0", "tools": entries}, indent=2, sort_keys=True) + "\n")
    print(len(entries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
```

- [x] Run it with the dispatcher-supplied `$BRIDGE` and the interpreter the dispatcher names: expect it to print `32` (the bridge registers 35 tools; 3 are dropped). (`identity()` at `$BRIDGE/dot_mcp.py:143` accepts any printable ASCII string up to 256 characters, so the placeholder passes.)
- [x] Open the fixture and confirm each entry has `name`, `description`, `inputSchema` and `annotations`, and that the `dot_t3_capabilities` description still mentions the dropped review tools only if the bridge text does; Task 8 rewrites that description, and the test in Task 9 compares descriptions for every tool except `dot_t3_capabilities`.
- [x] Guard-scan the fixture; replace any private host name in description text with the placeholder the docs use, and note each replacement in the commit message. Commit: `git add tests/fixtures/miller_tools_snapshot.json && git commit -m "test(miller): freeze the bridge tool snapshot"`.

## Task 2: contracts

**Files:** Create `src/brigade/miller/__init__.py`, `src/brigade/miller/contracts.py`, `tests/test_miller_contracts.py`.

- [x] Write the failing test:

```python
import json
from pathlib import Path

from brigade.miller import contracts as c

SNAPSHOT = json.loads((Path(__file__).parent / "fixtures" / "miller_tools_snapshot.json").read_text())


def test_tool_names_match_snapshot() -> None:
    assert c.TOOLS == frozenset(SNAPSHOT["tools"])


def test_read_only_flags_match_snapshot() -> None:
    for name, entry in SNAPSHOT["tools"].items():
        assert c.TOOL_SCHEMAS[name].read_only == entry["annotations"].get("readOnlyHint", False), name


def test_dropped_tools_absent() -> None:
    assert not {"dot_t3_submit_review", "dot_t3_job_status", "dot_t3_job_result"} & c.TOOLS


def test_gated_tools() -> None:
    assert c.GATED_TOOLS == frozenset(
        {"fleet_submit", "fleet_followup", "fleet_answer", "fleet_approve", "fleet_interrupt", "message_send"}
    )


def test_pack_constants() -> None:
    assert (c.PACK_ID, c.SERVICE_NAME, c.SERVER_VERSION, c.DEFAULT_BIND) == ("miller", "dot-t3", "0.28.0", "127.0.0.1:8779")
```

- [x] Run: `brigade work verify run --target . --command "python -m pytest tests/test_miller_contracts.py -q"`; expect collection error, no module `brigade.miller`.
- [x] Implement `contracts.py`: copy `$BRIDGE/dot_mcp.py` lines 82-132 (limits and error messages), the `ToolSchema` class (859-874) and the `TOOL_SCHEMAS` table (875-948) minus the three dropped entries and the `_choice` helper they use (`CONSUMER` at 928 stays). Add:

```python
PACK_ID = "miller"
SERVICE_NAME = "dot-t3"
SERVER_VERSION = "0.28.0"
DEFAULT_BIND = "127.0.0.1:8779"
TOOLS = frozenset(TOOL_SCHEMAS)
GATED_TOOLS = frozenset(
    {"fleet_submit", "fleet_followup", "fleet_answer", "fleet_approve", "fleet_interrupt", "message_send"}
)


class MillerError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
```

Remove every module-level path constant (`NATIVE_PYTHON`, `PILOT`, `STATE_DIRECTORY`, `PILOT_LEDGER`, `PROJECT`, `SNAPSHOT`, `T3_FLEET`, `BRIGADE`, `GIT`, `OCTOPOOL`, `GH`, `WORKSPACE`, `FLEET_CONFIG`, `DOT_NOTIFY`, `CHILD_PATH`, `DIGEST_PR_REPO`); they move to `Runtime` in Task 3. `__init__.py` exports `PACK_ID`, `TOOLS`, `MillerError` for now.
- [x] Run to green: same command, expect `5 passed`. Run `ruff check src/brigade/miller tests/test_miller_contracts.py` and `mypy src/brigade/miller` through verify; expect clean.
- [x] Commit: `feat(miller): add pack contracts and tool schemas`.

## Task 3: runtime config

**Files:** Create `src/brigade/miller/runtime_config.py`, `tests/test_miller_runtime.py`.

Runtime JSON shape (all keys required unless marked optional):

```json
{
  "schema": "brigade.miller.runtime.v1",
  "auth": {"issuer": "https://team.example.test", "team_domain": "team.example.test",
           "audience": "<64 hex>", "approved_email": "miller@example.test",
           "resource": "https://miller.example.test/mcp"},
  "paths": {"state_dir": "/abs", "workspace": "/abs", "fleet_config": "/abs/config.toml",
            "bin": {"t3_fleet": "/abs", "brigade": "/abs", "git": "/abs", "gh": "/abs", "octopool": "/abs"}},
  "hub": {"principal": "agent:miller", "node_id": "<uuid>", "message_token_file": "/abs",
          "poll_seconds": 30, "recipients": {"alias": "<uuid or agent:name>"}},
  "dispatch_enabled": true, "events_enabled": true,
  "digest_pr_repo": "owner/repo", "callback_hosts": ["connectors.api.openai.com"],
  "notify_command": ["/abs/notifier", "--hook", "custom"],
  "owner_authorization": {"version": "1", "text": "..."}
}
```

`paths.bin.octopool` is optional. `notify_command` is optional (watch alerts are skipped when absent).

- [x] Write failing tests: a valid document parses to `Runtime` with each field; each of these raises `MillerError("invalid_config")`: unknown top-level key, missing `auth.approved_email`, relative path anywhere in `paths`, `audience` not 64 lowercase hex, `poll_seconds` 9 and 301, 17 recipients, recipient value not matching `^(agent:[a-z][a-z0-9._-]{0,62}|[0-9a-f-]{36})$`, `principal` not starting `agent:`, `callback_hosts` empty, file mode 0644 (via `load_runtime`), symlinked file. Write the document with `os.open(..., 0o600)` inside `miller_private_dir`.
- [x] Run, expect failures (module missing).
- [x] Implement `parse_runtime(payload) -> Runtime` and `load_runtime(path) -> Runtime` (reads with `brigade.grokbot_t3.runtime_config.read_owner_file(path, max_bytes=65_536)`, maps its `T3DispatchError` to `MillerError("invalid_config")`), and `validate_runtime_reference(value) -> str` (absolute path, loads it, returns it unchanged). `Runtime` is a frozen dataclass with fields named after the JSON keys, nested `AuthConfig`, `PathsConfig`, `HubConfig` dataclasses, and `__repr__` that omits `owner_authorization.text` and the token file path.
- [x] Run to green; ruff and mypy clean. Commit: `feat(miller): parse the owner-only runtime config`.

## Task 4: Access JWT verifier

**Files:** Create `src/brigade/miller/access_auth.py`, `tests/test_miller_access_auth.py`.

- [x] Port tests from `$BRIDGE/tests/test_legacy_dot_auth.py` classes `PublicKeyTransportChecks`, `SignatureChecks` and `CompositionChecks` (lines 65-271, 272-430, 492-end). Replace the hard-coded issuer, email, resource and team host with values from a test `AuthConfig` (`team.example.test`, `miller@example.test`). Drop `ConfigurationChecks` (431-491); Task 3 covers config validation.
- [x] Run, expect failures.
- [x] Implement by copying `$BRIDGE/dot_auth.py` lines 140-308 (key transport, JWKS cache, `verify`), with these exact edits: the constructor takes `AuthConfig`; `ISSUER`, `EMAIL`, `RESOURCE`, `AUTHORIZATION_SERVER` and the team host at line 151 read from it; `import jwt` moves inside the function that first needs it and raises `grokbot_mcp.OptionalDependencyError` on `ImportError`; delete `validate_config` and `validate_hub_token_file` (Task 3 owns config). Keep RS256-only, 300 s cache, `type == "app"`, exact email match.
- [x] Run to green; ruff and mypy clean. Commit: `feat(miller): verify Cloudflare Access tokens at the origin`.

## Task 5: safe state and bridge state

**Files:** Create `src/brigade/miller/safe_state.py`, `src/brigade/miller/process.py`, `src/brigade/miller/state.py`, `tests/test_miller_state.py`, `tests/test_miller_process.py`.

- [x] Port tests: `$BRIDGE/tests/test_bridge_v5.py` `V5CallLogState` (117-175) and `test_legacy_dot_mcp.py` `test_unsafe_directory_database_and_ancestors_rejected` (257-277) into `tests/test_miller_state.py`; `test_bridge_v2.py` `CommandRunnerChecks` and `BoundingChecks` into `tests/test_miller_process.py`. Add a state test that opens a fresh database and asserts the tables `inbox`, `replies`, `operations`, `call_log`, `hub_inbox`, `hub_state` and every table `_initialize_event_tables` creates exist. Every temp dir comes from `miller_private_dir`.
- [x] Run, expect failures.
- [x] Implement `safe_state.py` from `$BRIDGE/dot_mcp.py` lines 134-285 (errors, validators, redaction, `private_file`, `private_directory`, `trusted_directory`) and the `PrivateDatabase` class from 287-458. Do not copy `Store` or `PilotProcess`. Implement `process.py` from `$BRIDGE/dot_mcp.py` 574-678 (`CommandResult`, `child_environment`, `CommandRunner`) and 679-750 (`_scrub`, `_clamp`, `bound_result`, `parse_json_bytes`); `encoded_size` already lives in `contracts.py`, import it. `child_environment(path: str, extra: Mapping[str, str] | None = None)` takes the PATH value instead of the `CHILD_PATH` constant; callers build it as the de-duplicated parent directories of every `Runtime.paths.bin` entry followed by `/usr/bin:/bin`. Port `test_bridge_v2.py` `CommandRunnerChecks` (714-747) and `BoundingChecks` (635-663) into `tests/test_miller_process.py`. Implement `state.py` from 1021-1256 (`BridgeState`). Delete the local inbox methods `notify`, `reply`, `replies`, `messages` and `ack`; keep the `CREATE TABLE` statements for `inbox` and `replies` so the old bridge can still open the database (spec decision 5). `state.py` owns every table: copy the body of `$BRIDGE/dot_events.py` `initialize(connection)` (line 160 onward) into `state.py` as `_initialize_event_tables(connection)` and call it where the bridge called `dot_events.initialize`. `BridgeState.__init__(self, state_dir: Path)` takes the directory from `Runtime.paths.state_dir`.
- [x] Run to green. Commit: `feat(miller): port private state and the bridge database`.

## Task 6: events and webhooks

**Files:** Create `src/brigade/miller/events.py`, `src/brigade/miller/webhooks.py`, `tests/test_miller_events.py`.

- [x] Port tests: `test_bridge_v3.py` `EventChecks`, `EventIntegrationChecks`, `EventRotationChecks`, `WebhookTransportChecks` (62-558); drop `MainModuleIdentityChecks` (559-end, it tests the `__main__` alias hack being removed).
- [x] Run, expect failures.
- [x] Implement `webhooks.py` from the signing and delivery half of `$BRIDGE/dot_events.py` (Standard Webhooks signature, h11-over-ssl POST, allowed host check reading `Runtime.callback_hosts` instead of `CALLBACK_HOSTS`). Implement `events.py` from `EventService` (198-627) and `EventsApp` (628-775). Replace every lazy `import dot_mcp` with direct imports from `brigade.miller.state` and `brigade.miller.safe_state`; the `IDENTITY` `ContextVar` moves to `events.py` and `lifecycle.py` imports it from there.
- [x] Run to green. Commit: `feat(miller): port MCP events and webhook delivery`.

## Task 7: fleet tools over FleetRunner

**Files:** Create `src/brigade/miller/fleet_tools.py`, `tests/test_miller_fleet_tools.py`.

- [x] Port tests from `test_bridge_v2.py` `FleetArgvChecks` (317-482), `EnrolledSubmitChecks` (483-504), `DispatchGateChecks` (284-307) and `test_bridge_v6.py` `CompletionChecks` (650-697). Rewrite every argv assertion as an assertion on the JSON payload the fake runner receives. The fake replaces `FleetRunner._run` (see `tests/test_grokbot_t3_fleet.py` for the pattern). For `fleet_submit` the payload must equal:

```python
{"schema": "t3-fleet.request.v1", "action": "submit", "host": "alpha", "repository": "brigade",
 "source_revision": SHA, "title": "t", "prompt": "<prompt with owner authorization appended>",
 "runtime_mode": "full-access", "interaction_mode": "default", "seat": "sol61",
 "initiator_kind": "agent", "initiator_label": "miller"}
```

with `workload`, `override_reason` and `allow_queue` present only when given.
- [x] Run, expect failures.
- [x] Implement from `$BRIDGE/dot_mcp.py` 1538-1712 (`_fleet`, submit, status, wait, recent, `_control`). Each call becomes `runner.call(action, payload, timeout=...)` on a `FleetRunner(runtime.paths.bin.t3_fleet, runtime.paths.bin.git, runtime_mode="full-access")`. Keep the source revision resolution (1581-1600) using `runtime.paths.bin.git` and `runtime.paths.fleet_config`. Keep the bridge's result shapes from `$BRIDGE/dot_mcp.py:1538-1556`: success is `{"ok": True, "result": value}`; `FleetCallError` becomes `{"error": "fleet_error", "fleet": {"code": err.code, "reason": err.reason}}` (drop `reason` when it is `None`) plus `request_id` when the error carries one; `FleetTimeout` becomes `{"error": "fleet_uncertain", "next": "Do not resubmit. Check fleet_recent and fleet_status for the request."}` for mutating calls (submit, followup, answer, approve, interrupt) and `{"error": "fleet_timeout"}` otherwise; `FleetProtocolError` becomes `{"error": "fleet_unavailable"}`. The t3-fleet error message text is no longer available through `FleetRunner`, so the operations journal records the code instead of the message.
- [x] Run to green. Commit: `feat(miller): drive t3-fleet through FleetRunner`.

## Task 8: hub, memory and inbox tools

> Execution note (2026-10-17): Task 8 was split into 8a (inbox_tools.py and hub_inbox.py over the message API, committed 5a1d8bfe) and 8b (hub_tools.py, memory_tools.py, adapter.py).

**Files:** Create `src/brigade/miller/hub_tools.py`, `memory_tools.py`, `inbox_tools.py`, `adapter.py`, and their three test files.

- [x] Port tests: `test_bridge_v2.py` `OverviewAndMemoryChecks` (505-578), `HandoffChecks` (579-634), `InboxChecks` (664-713, hub-mode cases only), `CompositionChecks` (748-end); `test_bridge_v4.py` all; `test_bridge_v5.py` `V5Digest` (262-385); `test_bridge_v6.py` `HubInboxChecks` (157-649) and `InboxTrustChecks` (1083-end). Replace `FakeHubRunner` (argv for `brigade fleet msg`) with a fake of `brigade.fleet_client_messages` functions patched via `monkeypatch.setattr`.
- [x] Run, expect failures.
- [x] Implement `hub_tools.py` from 1714-1895 and 2014-2156; `brigade` and `gh`/`octopool` binaries come from `runtime.paths.bin`, the PR repo from `runtime.digest_pr_repo`. Implement `memory_tools.py` from 1897-2012 plus `$BRIDGE/dot_fleet.py` (policy files resolve under `runtime.paths.workspace`; the default authorization text comes from `runtime.owner_authorization.text`). Implement `inbox_tools.py` from 2158-2176 plus `$BRIDGE/dot_hub.py`, replacing every `brigade fleet msg` subprocess with `fleet_client_messages.send/list_messages/ack/reply/sent(..., token_file=runtime.hub.message_token_file)`; alias resolution uses `runtime.hub.recipients`. Implement `adapter.py`: `MillerTools.call(name, arguments)` checks `GATED_TOOLS` against `runtime.dispatch_enabled` (raise `MillerError("dispatch_disabled")`), dispatches to the module, records the call log entry (copy the summary logic from `$BRIDGE/dot_mcp.py` 949-1019), and serves `dot_t3_capabilities` from 1412-1475 with the three dropped tools removed from its text and host names replaced by values from `runtime.hub.recipients`.
- [x] Run to green. Commit: `feat(miller): port hub, memory and inbox tools`.

## Task 9: lifecycle, watch and entry points

**Files:** Create `src/brigade/miller/lifecycle.py`, `watch.py`, `__main__.py`, `tests/test_miller_lifecycle.py`, `tests/test_miller_watch.py`.

- [x] Port tests: `test_legacy_dot_mcp.py` `AuthenticationChecks` (287-313); `test_bridge_v6.py` `ConfigChecks` (698-757) and `ScriptChecks` (758-911, the `notify`, `calls` and `watch` cases; drop migration-script cases); `test_bridge_v5.py` `V5CallLogComposition` (201-261) and `V5Scripts` (386-end). Add these new tests:
  - `test_only_two_routes_admitted`: any path other than `/mcp` (POST) and `/.well-known/oauth-protected-resource/mcp` (GET) returns 404 before the verifier runs.
  - `test_host_and_origin_rejected_before_auth`: wrong Host returns 403 and the verifier mock is not called.
  - `test_missing_jwt_returns_401_with_resource_metadata`.
  - `test_render_unit` asserts `ExecStart` runs `-m brigade run cloud grokbot serve --pack miller --target <t> --bind <b>` with `--allow-host`/`--allow-origin` flags and no bearer flag, and the hardening lines from Pinned decisions.
  - `test_canary_checks_anonymous_401_metadata_and_registry`.
  - `test_registered_tools_match_snapshot`: build the app with a fake `MillerTools`, capture the `MCPServer` the same way Task 1's script does, and assert `tool.model_dump(mode="json", by_alias=True, exclude_none=True)` equals the fixture entry for every tool, comparing every field for every tool except the `description` of `dot_t3_capabilities`.
- [x] Run, expect failures.
- [x] Implement `lifecycle.py`, structured like `src/brigade/grokbot_t3/lifecycle.py`:
  - `MillerListenerConfig(target, bind_host, bind_port, allowed_hosts, allowed_origins, runtime_path)`.
  - `_MillerGate.reject_reason(path, method, headers)`: route check (404), size, Host and Origin (403, same rules as `_T3RequestGate`), then `AccessVerifier.verify(headers.get("cf-access-jwt-assertion"))` (401 with `www-authenticate: Bearer resource_metadata="<resource>/.well-known/oauth-protected-resource/mcp"` as the bridge sends at `$BRIDGE/dot_mcp.py:2178-2306`).
  - `build_app(config, tools, runtime)`: `MCPServer(c.SERVICE_NAME, version=c.SERVER_VERSION)`; register the 32 tools by copying their `@server.tool(...)` blocks and function signatures verbatim from `$BRIDGE/dot_mcp.py:2345-2565` (minus the three dropped tools), with each body calling `MillerTools.call`; the `annotations()` helper and the `invoke()` call-log wrapper come from 2321-2348; the resource metadata route, the events app wrapper from Task 6, `streamable_http_app(json_response=True, stateless_http=True, ...)`, wrapped in the gate.
  - `run_listener`, `build_listener_from_target(target, *, bind, allowed_hosts, allowed_origins)`, `doctor(target)` (checks: dependency, config, state, access-config, hub-token, t3-fleet capacity), `canary(target)` (anonymous `POST /mcp` returns 401, metadata document answers with the configured resource, `set(registered tool names) == contracts.TOOLS`), `render_unit`, `write_unit`, `render_watch_units`, `write_watch_units` (unit text per Pinned decisions).
- [x] Implement `watch.py` from `$BRIDGE/dot-inbox-watch` with the five checks and dedupe state file `inbox-watch.json` in the state dir; probe URLs come from the instance bind and `runtime.auth.resource`; alerts run `runtime.notify_command` with the message on stdin.
- [x] Implement `__main__.py`: `serve` (delegates to `run_cloud` serve), `notify` (from `$BRIDGE/dot-notify`, hub mode only), `calls` (from `$BRIDGE/bridge-calls`), `watch --target <t>`.
- [x] Run to green. Commit: `feat(miller): add the listener, watch and entry points`.

## Task 10: registry, CLI and docs

**Files:** Modify `src/brigade/grokbot_packs.py`, `src/brigade/cli/run_cloud.py`, `tests/test_grokbot_packs.py`; create `docs/grokbot-miller.md`.

- [x] Write failing tests in `tests/test_grokbot_packs.py`: `show_pack("miller")` returns kind `connector`, bind `127.0.0.1:8779`, tools equal `brigade.miller.TOOLS`; `preview_setup(target, "miller", runtime_path=<valid>)` writes no `bearer` key; passing `bearer_file` for `miller` raises `PackError("unexpected-key")`; an instance file with a `bearer` key fails validation for `miller`; `doctor`, `canary` and `render_install_service` delegate to `brigade.miller` (monkeypatched).
- [x] Run, expect failures.
- [x] Implement in `grokbot_packs.py`:
  - `MILLER_PACK_ID = "miller"`; add `"miller": "127.0.0.1:8779"` to `CONNECTOR_DEFAULT_BINDS`; `MILLER_INSTANCE_KEYS = (INSTANCE_KEYS - {"bearer"}) | frozenset({"runtime_path"})`.
  - `_connector_tools`: `if pack_id == MILLER_PACK_ID: return miller.TOOLS`.
  - `_instance_keys`: `if pack["id"] == MILLER_PACK_ID: return MILLER_INSTANCE_KEYS`.
  - `_setup`: `reference = None if pack_id == MILLER_PACK_ID else _bearer_reference(...)`; for Miller raise `PackError("unexpected-key")` if any bearer argument is given; build `payload` without `"bearer"` when `reference is None`; add the Miller branch next to the t3 branch: reject every non-runtime path argument, set `payload["runtime_path"] = _miller_runtime_reference(runtime_path)`.
  - `_validate_instance_config`: `if "bearer" in expected_keys: _validate_bearer_reference(payload.get("bearer"))`; add `elif pack["id"] == MILLER_PACK_ID: _miller_runtime_reference(payload.get("runtime_path"))`.
  - `_miller_runtime_reference` mirrors `_t3_runtime_reference`, mapping `miller.MillerError` to `PackError("unsafe-path")`.
  - `doctor`, `canary`, `render_install_service`, `apply_install_service`: `elif pack["id"] == MILLER_PACK_ID:` branches calling `miller.doctor`, `miller.canary`, `miller.render_unit(target, python=sys.executable)`, `miller.write_unit(target, out_dir, force=force)`, adding `miller.MillerError` to the caught exception tuples next to `grokbot_t3.T3DispatchError`.
- [x] Implement in `run_cloud.py` the `serve --pack miller` branch next to the t3 branch at about line 1194: reject `--bearer-file`/`--bearer-env` with the existing usage error, then `config, tools, runtime = miller.build_listener_from_target(target, bind=..., allowed_hosts=..., allowed_origins=...)` and `miller.run_listener(config, tools, runtime)`; add `miller.MillerError` to the caught tuple near line 1244.
- [x] Write `docs/grokbot-miller.md` with the sections of `docs/grokbot-t3-dispatch.md`: purpose, runtime JSON table (from Task 3), setup commands (`pack setup miller --runtime-path ...`, `pack doctor`, `pack canary`, `pack install-service`), the watch timer, the tool table (32 tools, gated ones marked), and the security model (Access JWT at the origin, no bearer, Host/Origin allowlists, state directory checks). Placeholders only for hosts and paths.
- [x] Run to green: `python -m pytest tests/test_grokbot_packs.py tests/test_miller_*.py -q`, then `./scripts/verify-focused tests/test_grokbot_packs.py tests/test_miller_contracts.py`, the size ratchet test, and the guard scan on every new or changed file. Commit: `feat(miller): register the Miller connector pack`.

## Task 11: cutover (operator, not a worker)

Run by the orchestrating session after the PR merges, following the spec's Cutover section exactly, with these commands:

- [ ] `pipx install --force <origin/main worktree>[grokbot]` into the main venv (memory: install from a detached origin/main worktree, re-inject as needed).
- [ ] Write the runtime JSON from the bridge config (mode 0600), `brigade run cloud grokbot pack setup miller --target <workspace> --runtime-path <file> --allow-host <public host> --allow-origin <public origin>`, then `pack doctor miller` (all ok).
- [ ] Copy the state directory to a scratch private directory, point a scratch runtime file at it, run `serve --pack miller --bind 127.0.0.1:8779`, run `pack canary miller`; expect `ok: true`.
- [ ] `systemctl --user stop dot-t3-mcp.service`; `pack setup miller --bind 127.0.0.1:8787 ...` against the real state directory; `pack install-service miller`; `systemctl --user daemon-reload && systemctl --user enable --now brigade-miller.service`.
- [ ] Anonymous `POST` to the public `/mcp` returns 401. Hub message to Miller asking for `dot_t3_capabilities` and `fleet_capacity`; confirm both rows with `python -m brigade.miller calls`.
- [ ] Disable `dot-inbox-watch.timer`, enable `brigade-miller-watch.timer`.
- [ ] Rollback if any step fails: `systemctl --user disable --now brigade-miller.service && systemctl --user start dot-t3-mcp.service`.
- [ ] One week later: tar the bridge directory into the backup area, remove it, write the Memory Handoff.
