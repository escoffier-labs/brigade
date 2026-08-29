# Fleet interactive session presence implementation plan

## Goal

Implement issue #1261 from
`docs/phase-fleet-interactive-session-presence.md`: the Fleet Hub becomes the
authoritative view of interactive Claude, Codex/T3, and Cursor session presence,
with bounded dirty paths and advisory overlap warnings.

Execute tasks in order and tick every checkbox as it completes. Each behavior
change starts with a failing test, uses the smallest implementation that passes,
and is committed before the next task. Do not add dependencies, daemons,
exclusive session locks, fleet plans, or diff transport.

## Architecture

`fleet_session_presence.py` owns local Git identity, snapshots, and pure overlap
projection. `fleet_hub_sessions.py` owns schema version 14 session state and
transactions. The existing Hub, HTTP, client, CLI, hooks, work brief, and Deck
modules only adapt those two domains. Node credentials remain the write identity.

## File map

| File | Responsibility |
| --- | --- |
| Create `src/brigade/fleet_session_presence.py` | Safe local identity, dirty paths, session snapshots, overlap projection. |
| Create `src/brigade/fleet_hub_sessions.py` | Schema, validation, upsert/end transactions, safe list. |
| Modify `src/brigade/fleet_hub.py` | Schema version 14 initialization and run-event `repo_identity`. |
| Modify `src/brigade/fleet_hub_http.py` | Authenticated `/sessions` routes. |
| Modify `src/brigade/fleet_client.py` | Session transport and run-event identity. |
| Modify `src/brigade/cli/fleet.py` | `brigade fleet sessions`. |
| Modify `src/brigade/cli/work/registration.py` | Internal managed presence-hook parser. |
| Modify `src/brigade/cli/work/dispatching.py` | Internal managed presence-hook dispatch. |
| Modify `src/brigade/claude_hooks/runtime.py` | Claude start, refresh, and accepted-stop publication. |
| Modify `src/brigade/cursor_user_cmd.py` | Cursor start, refresh, and end publication. |
| Modify `src/brigade/work_cmd/session/briefing.py` | Codex/T3 startup publication and overlap warnings. |
| Modify `src/brigade/fleet_command_deck.py` | Separate session panel and repo overlap marker. |
| Create `tests/test_fleet_session_presence.py` | Identity, dirty path, overlap, and Hub-domain tests. |
| Modify `tests/test_fleet_sync.py` | HTTP, auth, migration, client, and run identity tests. |
| Modify `tests/test_fleet_claims.py` | Schema-upgrade compatibility assertions. |
| Modify `tests/test_claude_hooks_runtime.py` | Claude lifecycle publication tests. |
| Modify `tests/test_claude_hooks_envelope.py` | Hook stdout and failure isolation tests. |
| Modify `tests/test_cursor_user_cmd.py` | Generated hook publication contract. |
| Modify `tests/test_work_cmd_session.py` | Codex/T3 presence and overlap brief tests. |
| Modify `tests/test_fleet_command_deck.py` | Session panel, escaping, and capacity tests. |

## Blast radius

`brigade code affected` on the six existing core modules returned 116 impacted
files and 122 statically attributed test groups. Direct behavior-lock suites are
the eight test files named above plus `tests/test_fleet_dashboard.py` and
`tests/test_cli_inventory_contract.py`. The implementation must run through
`brigade run` so `run.json` records the code-graph brief.

### Task 1: local snapshot and Hub session authority

**Files:**

- Create `src/brigade/fleet_session_presence.py`
- Create `src/brigade/fleet_hub_sessions.py`
- Modify `src/brigade/fleet_hub.py`
- Create `tests/test_fleet_session_presence.py`
- Modify `tests/test_fleet_claims.py`

- [x] Write failing local identity and dirty-path tests using only fake remotes.

```python
def test_remote_forms_have_one_credential_free_identity(git_repo, monkeypatch):
    identities = []
    for remote in (
        "https://github.com/example/project.git",
        "https://user:secret@github.com/example/project.git",  # content-guard: allow email
        "ssh://git@github.com/example/project.git",  # content-guard: allow email
        "git@github.com:example/project.git",  # content-guard: allow email
    ):
        _set_origin(git_repo, remote)
        identities.append(presence.repository_identity(git_repo))
    assert {item.value for item in identities} == {"github.com/example/project"}
    assert all(item.scope == "fleet" for item in identities)
    assert "secret" not in repr(identities)

def test_dirty_paths_are_bounded_relative_and_rename_safe(git_repo):
    _seed_dirty_paths(git_repo, count=70, include_rename=True)
    snapshot = presence.collect_dirty_paths(git_repo)
    assert len(snapshot.paths) == 64
    assert snapshot.truncated is True
    assert all(not Path(path).is_absolute() and ".." not in Path(path).parts for path in snapshot.paths)
    assert "renamed file.txt" in snapshot.paths
    assert all(not path.startswith(".brigade/") for path in snapshot.paths)
```

- [x] Run red through Brigade:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_fleet_session_presence.py"]' --capture brigade-work
```

Expect collection failure because `brigade.fleet_session_presence` does not
exist. Capture the failed outcome before implementing.

- [x] Implement the local immutable shapes and exact public functions.

```python
DEFAULT_TTL_SECONDS = 900
MIN_TTL_SECONDS = 120
MAX_TTL_SECONDS = 3600
MAX_DIRTY_PATHS = 64
MAX_DIRTY_PATH_CHARS = 512
MAX_DIRTY_JSON_BYTES = 32 * 1024

@dataclass(frozen=True)
class RepositoryIdentity:
    value: str
    scope: Literal["fleet", "node"]

@dataclass(frozen=True)
class DirtyPathSnapshot:
    paths: tuple[str, ...]
    truncated: bool

@dataclass(frozen=True)
class SessionSnapshot:
    harness: str
    session_id: str
    repo_identity: str
    identity_scope: str
    repo_label: str
    checkout_path: str
    branch: str | None
    dirty_paths: tuple[str, ...]
    dirty_truncated: bool
    ttl_seconds: int = DEFAULT_TTL_SECONDS

```

The exact public functions are `repository_identity(target: Path) ->
RepositoryIdentity`, `collect_dirty_paths(target: Path) -> DirtyPathSnapshot`,
`build_snapshot(target: Path, *, harness: str, session_id: str, ttl_seconds: int
= DEFAULT_TTL_SECONDS) -> SessionSnapshot`, and `overlap_warnings(current:
SessionSnapshot, rows: Sequence[Mapping[str, object]], *, current_node: str) ->
list[dict[str, object]]`.

Use `subprocess.run(["git", "remote", "get-url", "origin"], shell=False,
timeout=5, cwd=target)` for the remote and `subprocess.run(["git", "status",
"--porcelain=v1", "-z", "--untracked-files=all"], shell=False, timeout=5,
cwd=target)` for paths. Parse NUL records, consume
the second path for rename/copy status, normalize separators to `/`, sort, and
deduplicate. Do not call `runguard.dirty_paths`, whose line parser does not carry
the required NUL and truncation contract.

- [x] Write failing schema, transaction, and migration tests.

```python
def test_schema_13_migrates_to_14_without_touching_existing_rows(tmp_path):
    db = _schema_13_database(tmp_path)
    conn = fleet_hub.init_db(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 14
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM interactive_sessions").fetchone()[0] == 0

def test_upsert_retry_refresh_end_and_expiry(conn, monkeypatch):
    first = sessions.handle_session(conn, _upsert(), caller_node=NODE_A)
    retry = sessions.handle_session(conn, _upsert(branch="topic"), caller_node=NODE_A)
    assert retry[1]["session"]["started_at"] == first[1]["session"]["started_at"]
    assert retry[1]["session"]["branch"] == "topic"
    assert sessions.handle_session(conn, _end(), caller_node=NODE_A)[0] == 200
    assert sessions.list_sessions(conn) == []
    assert sessions.list_sessions(conn, include_all=True)[0]["state"] == "ended"
```

- [x] Implement `fleet_hub_sessions.py` with these entry points.

```python
SCHEMA = """
CREATE TABLE IF NOT EXISTS interactive_sessions (
    node_id TEXT NOT NULL,
    harness TEXT NOT NULL,
    session_id TEXT NOT NULL,
    repo_identity TEXT NOT NULL,
    identity_scope TEXT NOT NULL,
    repo_label TEXT NOT NULL,
    checkout_path TEXT NOT NULL,
    branch TEXT,
    dirty_paths_json TEXT NOT NULL,
    dirty_truncated INTEGER NOT NULL,
    state TEXT NOT NULL,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    ended_at TEXT,
    ttl_seconds INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (node_id, harness, session_id, repo_identity)
);
"""
```

The exact public entry points are `init_schema(conn: sqlite3.Connection) ->
None`, `handle_session(conn: sqlite3.Connection, raw: object, *, caller_node:
str | None) -> tuple[int, dict[str, object]]`, and `list_sessions(conn:
sqlite3.Connection, *, include_all: bool = False, now_epoch: float | None =
None) -> list[dict[str, object]]`.

`handle_session` accepts only `upsert` and `end`. Validate the complete body,
derive the owner from `caller_node`, then use one `BEGIN IMMEDIATE` transaction.
An admin compatibility write has `caller_node=None` and therefore requires a
validated body `node_id`; normal node writes reject body `node_id`. Public rows
never include private auth material. Default reads cap at 500, history at 1,000.

- [x] Set `fleet_hub.SCHEMA_VERSION = 14`, call
  `fleet_hub_sessions.init_schema(conn)` from the startup migration, and add the
  nullable `repo_identity` events column. Keep every prior migration additive.

- [x] Run Task 1 green through Brigade:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_fleet_session_presence.py","tests/test_fleet_claims.py"]' --capture brigade-work
```

Expect exit 0 and all selected tests passed.

- [x] Commit:

```bash
git add src/brigade/fleet_session_presence.py src/brigade/fleet_hub_sessions.py src/brigade/fleet_hub.py tests/test_fleet_session_presence.py tests/test_fleet_claims.py
git commit -m "feat(fleet): add interactive session authority" -m "Co-Authored-By: Cursor <cursoragent@cursor.com>"  # content-guard: allow email
```

### Task 2: HTTP, client, CLI, and run identity

**Files:**

- Modify `src/brigade/fleet_hub_http.py`
- Modify `src/brigade/fleet_client.py`
- Modify `src/brigade/cli/fleet.py`
- Modify `tests/test_fleet_sync.py`
- Modify `tests/test_cli_inventory_contract.py`

- [x] Write failing HTTP and auth tests.

```python
def test_session_routes_auth_and_node_identity(hub_server):
    assert _request(hub_server, "GET", "/sessions")[0] == 401
    assert _request(hub_server, "POST", "/sessions", body=_upsert(node_id=NODE_B), token=NODE_A_TOKEN)[0] == 400
    assert _request(hub_server, "POST", "/sessions", body=_upsert(), token=NODE_A_TOKEN)[0] == 200
    status, body = _request_json(hub_server, "GET", "/sessions", token=NODE_A_TOKEN)
    assert status == 200 and body["sessions"][0]["node_id"] == NODE_A
    assert _request(hub_server, "POST", "/sessions", body=_upsert(), cookie=_dashboard_cookie())[0] == 401

def test_revoked_node_cannot_refresh_session(hub_server):
    _create_session(hub_server, NODE_A_TOKEN)
    _revoke_node(hub_server, NODE_A)
    assert _request(hub_server, "POST", "/sessions", body=_upsert(), token=NODE_A_TOKEN)[0] == 401
```

- [x] Run the two tests through Brigade and expect 404 failures for `/sessions`.

- [x] Route `GET /sessions` beside the authenticated status reads and
  `POST /sessions` beside node-authenticated writes. Node tokens may read and
  mutate their own rows. Admin reads are allowed. Admin writes retain the
  existing `allow_admin_writes` gate. Cookies do not reach either route.

- [x] Write failing client and CLI tests.

```python
def test_client_upsert_end_and_fetch_use_node_token(monkeypatch, session_snapshot):
    calls = _record_hub_calls(monkeypatch)
    assert fleet_client.upsert_session(session_snapshot) is True
    assert fleet_client.end_session(session_snapshot) is True
    assert fleet_client.fetch_sessions() == [_safe_session_row()]
    assert [call.path for call in calls] == ["/sessions", "/sessions", "/sessions"]
    assert all("Authorization" in call.headers for call in calls)

def test_fleet_sessions_cli_is_bounded_and_safe(monkeypatch, capsys):
    monkeypatch.setattr(fleet_client, "fetch_sessions", lambda **_: [_safe_session_row()])
    assert cli.main(["fleet", "sessions", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["repo_identity"] == "github.com/example/project"
    assert "dirty_paths" in payload[0]
```

- [x] Implement exact client entry points. Mutations return `False` on any
  ordinary exception and never spool. Reads raise `FleetClientError`.

The exact entry points are `upsert_session(snapshot: SessionSnapshot, *,
hub_url: str | None = None) -> bool`, `end_session(snapshot: SessionSnapshot, *,
hub_url: str | None = None) -> bool`, and `fetch_sessions(*, hub_url: str | None
= None, include_all: bool = False) -> list[dict[str, Any]]`.

- [x] Add `brigade fleet sessions [--all] [--json]`. Text columns are NODE,
  HARNESS, SESSION, REPO, BRANCH, DIRTY, AGE, EXPIRES. Pass every cell through
  `_safe_table_cell`. JSON prints only the list returned by `fetch_sessions`.

- [x] Add `repo_identity` to event validation, insertion, status projection,
  and client event construction. `report_journal_event` derives it from the
  journal workspace with `repository_identity`; failures leave it `None`.

- [x] Run Task 2 green through Brigade:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_fleet_sync.py","tests/test_fleet_claims.py","tests/test_cli_inventory_contract.py","tests/test_fleet_session_presence.py"]' --capture brigade-work
```

- [x] Commit:

```bash
git add src/brigade/fleet_hub_http.py src/brigade/fleet_client.py src/brigade/cli/fleet.py src/brigade/fleet_hub.py tests/test_fleet_sync.py tests/test_fleet_claims.py tests/test_cli_inventory_contract.py tests/test_fleet_session_presence.py
git commit -m "feat(fleet): expose interactive sessions" -m "Co-Authored-By: Cursor <cursoragent@cursor.com>"  # content-guard: allow email
```

### Task 3: harness publication and overlap briefing

**Files:**

- Modify `src/brigade/cli/work/registration.py`
- Modify `src/brigade/cli/work/dispatching.py`
- Modify `src/brigade/claude_hooks/runtime.py`
- Modify `src/brigade/cursor_user_cmd.py`
- Modify `src/brigade/work_cmd/session/briefing.py`
- Modify `tests/test_claude_hooks_runtime.py`
- Modify `tests/test_claude_hooks_envelope.py`
- Modify `tests/test_cursor_user_cmd.py`
- Modify `tests/test_work_cmd_session.py`

- [x] Write failing managed presence-hook tests.

```python
def test_presence_hook_upserts_and_ends_without_printing_payload(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(fleet_client, "upsert_session", lambda snap: calls.append(("upsert", snap)) or True)
    monkeypatch.setattr(fleet_client, "end_session", lambda snap: calls.append(("end", snap)) or True)
    assert cli.main(["work", "presence-hook", "--target", str(tmp_path), "--harness", "codex", "--session", "thread-1", "--event", "start"]) == 0
    assert cli.main(["work", "presence-hook", "--target", str(tmp_path), "--harness", "codex", "--session", "thread-1", "--event", "end"]) == 0
    assert [item[0] for item in calls] == ["upsert", "end"]
    assert capsys.readouterr().out == ""
```

- [x] Add `work presence-hook` with two exact input modes. Explicit mode has
  required target, harness, session, and event choices `start`, `heartbeat`,
  `end`. Cursor mode is `--harness cursor --stdin --context cursor-work-loop`;
  it reads one bounded JSON object, derives the event from `hook_event_name`,
  takes `session_id` or `conversation_id`, and takes the first
  `workspace_roots` item. Both modes resolve the local snapshot and call the
  typed client. The command always exits 0 for adapter safety. Explicit mode
  prints nothing. Cursor mode prints the existing `additional_context` JSON for
  `sessionStart` and `{}` for `postToolUse` and `sessionEnd`. It never accepts
  node ID, repo identity, dirty paths, or checkout path from argv or JSON.

- [x] Write failing Claude lifecycle tests.

```python
def test_claude_presence_start_refresh_and_accepted_stop(target, monkeypatch):
    calls = _capture_presence(monkeypatch)
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id="s1"))
    runtime.handle_payload("PostToolUse", _write_payload(target, session_id="s1"))
    runtime.handle_payload("Stop", _payload(target, "Stop", session_id="s1", stop_hook_active=False))
    assert [call.action for call in calls] == ["upsert", "upsert", "end"]

def test_blocked_stop_does_not_end_presence(target, monkeypatch):
    calls = _capture_presence(monkeypatch)
    result = runtime.handle_payload("Stop", _unverified_write_stop(target, session_id="s1"))
    assert result["decision"] == "block"
    assert all(call.action != "end" for call in calls)
```

- [x] Add one `_publish_presence(event, target, session_id)` wrapper in
  `fleet_session_presence.py`. Claude runtime calls it after target/session
  validation. `SessionStart` calls `upsert`; relevant `PostToolUse` calls
  `upsert` after state updates; Stop calls `end` only immediately before a
  successful `None` or non-blocking context return. Publication exceptions are
  swallowed and never alter the existing hook envelope.

- [x] Write failing Cursor hook tests, then change `_hook_text()` to the exact
  shell adapter `exec brigade work presence-hook --harness cursor --stdin
  --context cursor-work-loop`. Register the same managed command for
  `sessionStart`, `postToolUse`, and `sessionEnd`, preserving foreign entries in
  all three arrays. Cursor's documented common input supplies
  `conversation_id`, `hook_event_name`, and `workspace_roots`; session start/end
  also supply `session_id`. If identity or workspace is absent, publication is
  skipped and the required JSON output still prints. Tests must prove planted
  prompt, transcript, user-email, tool-output, and credential fields never
  appear in argv, stdout, logs, or a client snapshot.

- [x] Write failing work-brief tests for Codex/T3 and overlap.

```python
def test_work_brief_publishes_codex_thread_and_warns_on_exact_overlap(tmp_target, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-1")
    monkeypatch.setattr(fleet_client, "fetch_sessions", lambda **_: [_other_session(dirty_paths=["src/a.py"])])
    _make_dirty(tmp_target, "src/a.py")
    assert briefing.brief(target=tmp_target, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["interactive_sessions"]["published"] is True
    assert payload["overlap_warnings"][0]["paths"] == ["src/a.py"]
```

Resolve the current harness/session in this order: explicit managed hook fields,
`CODEX_THREAD_ID` as harness `codex` (including T3 Code), then no publication.
Brief publication is best-effort. Fetch failure adds a bounded
`interactive_sessions.error` and no warning. Text output prints at most three
warnings and eight paths each. JSON preserves the full bounded projection.

- [x] Run Task 3 green through Brigade:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_claude_hooks_runtime.py","tests/test_claude_hooks_envelope.py","tests/test_cursor_user_cmd.py","tests/test_work_cmd_session.py","tests/test_fleet_session_presence.py"]' --capture brigade-work
```

- [x] Commit:

```bash
git add src/brigade/fleet_session_presence.py src/brigade/cli/work/registration.py src/brigade/cli/work/dispatching.py src/brigade/claude_hooks/runtime.py src/brigade/cursor_user_cmd.py src/brigade/work_cmd/session/briefing.py tests/test_claude_hooks_runtime.py tests/test_claude_hooks_envelope.py tests/test_cursor_user_cmd.py tests/test_work_cmd_session.py tests/test_fleet_session_presence.py
git commit -m "feat(work): publish interactive fleet presence" -m "Co-Authored-By: Cursor <cursoragent@cursor.com>"  # content-guard: allow email
```

### Task 4: Command Deck projection, documentation, and completion gate

**Files:**

- Modify `src/brigade/fleet_command_deck.py`
- Modify `tests/test_fleet_command_deck.py`
- Modify `tests/test_fleet_dashboard.py`
- Modify `docs/fleet-sync.md`
- Modify `docs/technical-guide.md`
- Modify `docs/phase-fleet-interactive-session-presence-plan.md`

- [x] Write failing Deck tests.

```python
def test_deck_sessions_are_separate_escaped_and_do_not_consume_capacity(conn, now):
    _seed_session(conn, checkout_path='<script>alert(1)</script>', dirty_paths=["src/a.py"])
    sessions = deck.fetch_interactive_sessions(conn, now=now)
    view = deck.build_view(
        deck.DeckConfig(stations=(deck.StationConfig(NODE_A, "Alpha", 10),)),
        live_runs=[],
        claims=[],
        enrolled_labels={NODE_A: "Alpha"},
        last_heard={},
        outcomes=[],
        failed_outcomes=[],
        observers=[],
        now=now,
        interactive_sessions=sessions,
    )
    html = deck.render_deck(view, nonce="nonce", now=now)
    assert "Interactive sessions" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html
    assert view.stations[0].busy == 0

def test_repo_page_marks_only_exact_live_dirty_overlap(conn, now):
    _seed_session(conn, node=NODE_A, dirty_paths=["src/a.py"])
    _seed_session(conn, node=NODE_B, dirty_paths=["src/a.py"])
    sessions = deck.fetch_interactive_sessions(conn, now=now)
    view = deck.build_view(
        deck.DeckConfig(stations=(deck.StationConfig(NODE_A, "Alpha", 10),)),
        live_runs=[],
        claims=[],
        enrolled_labels={NODE_A: "Alpha", NODE_B: "Bravo"},
        last_heard={},
        outcomes=[],
        failed_outcomes=[],
        observers=[],
        now=now,
        interactive_sessions=sessions,
    )
    assert view.repos[0].interactive_overlap is True
```

- [x] Add a capped `fetch_interactive_sessions(conn, now)` query through
  `fleet_hub_sessions.list_sessions`, then project a separate panel. Never add
  these rows to `LiveRun`, station `busy`, cloud capacity, run collisions, or
  outcome history. Repo overlap uses the pure exact-path algorithm.

- [x] Document the command, data bounds, TTL semantics, credential boundary,
  no-spool rule, harness coverage, and the fact that warnings are advisory.
  Include `brigade fleet sessions --json` and `brigade work brief --json`
  examples containing only fake identities and paths.

- [x] Run the focused completion gate through Brigade:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_fleet_session_presence.py","tests/test_fleet_sync.py","tests/test_fleet_claims.py","tests/test_fleet_command_deck.py","tests/test_fleet_dashboard.py","tests/test_claude_hooks_runtime.py","tests/test_claude_hooks_envelope.py","tests/test_cursor_user_cmd.py","tests/test_work_cmd_session.py","tests/test_cli_inventory_contract.py"]' --capture brigade-work
```

Expect exit 0 with ruff, format, version sync, snapshots, mypy, and every
selected pytest file passing.

- [x] Tick every completed checkbox in this plan and commit:

```bash
git add src/brigade/fleet_command_deck.py tests/test_fleet_command_deck.py tests/test_fleet_dashboard.py docs/fleet-sync.md docs/technical-guide.md docs/phase-fleet-interactive-session-presence-plan.md
git commit -m "feat(fleet): show interactive session overlap" -m "Co-Authored-By: Cursor <cursoragent@cursor.com>"  # content-guard: allow email
```

- [x] Run the full completion gate once, without `--no-reuse`:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify"]' --timeout 3600 --capture brigade-work
```

Status 75 means another full gate owns this checkout. Do not retry it. Wait for
that receipt or report the gate as externally occupied.

The single completion run `20260829-185326-work-verify-043f4f` reached 90%
before the 3,600-second bound expired. Its three recorded failures were
diagnosed separately: one unrelated harness test passed unchanged, and two
Cloud Tracker tests were polluted by the operator host's live Jules key. The
fixture isolation repair passed in focused receipt
`20260829-195709-work-verify-85a7f2`. The full gate was not retried.

- [x] Inspect the final diff against this plan, run `git diff --check
origin/main...HEAD`, write and lint a Memory Handoff, and stop at a clean feature
branch ready for review. Do not deploy, push, open a PR, merge, or cut a release
without the user's next instruction.
