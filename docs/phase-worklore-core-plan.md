# Worklore core implementation plan

## Goal

Ship the isolated Worklore store, validation, native HTTP contract, burn
queue, and `brigade fleet work` CLI so a native fleet-maintenance item can
be created without a repository and appear in the burn queue after it
becomes ready. Execute the tasks in order and check every box.

Approved design: `docs/phase-worklore-ledger.md`.

This plan is independently executable. It does not implement the GitHub
adapter, the Brigade adapter, or Ops Deck. Those slices have their own
plans and talk to the import and read contracts defined here. This slice
is the only owner of `import_batch` and `list_items`.

## Integration precondition

`docs/runbooks/fleet-hub-proxmox.md` already uses "Phase 2" for the
shipped hub. `src/brigade/fleet_hub.py:1-2` documents issue #1123 as that
shipped phase. Do not block on an unnamed "active Fleet Hub phase 2
branch".

Tasks 1 through 5 add isolated modules and tests that do not edit
`fleet_hub.py` routing, `fleet_hub_http.py` method tables, or
`SCHEMA_VERSION`. Task 6 is the only integration task. Start it only when
all three checks pass:

1. Fresh `origin/main`. Run `git fetch origin` and rebase this branch so
   `git merge-base --is-ancestor origin/main HEAD` succeeds.
2. No live conflicting owner. Run `brigade fleet claims --json` and
   confirm no unexpired claim names this checkout, `src/brigade/fleet_hub.py`,
   or `src/brigade/fleet_hub_http.py` as its target.
3. Operator confirmation. Record in the Task 6 checkbox that the operator
   has said the warned concurrent Fleet Hub work has landed. Do not infer
   that from a branch name or from `SCHEMA_VERSION`.

Then set the Worklore schema bump to the then-current `SCHEMA_VERSION + 1`
and add `/work` routes without rewriting events, claims, nodes, cloud,
model, grokbot, or preference tables.

Current `origin/main` and this worktree use `SCHEMA_VERSION = 13` in
`src/brigade/fleet_hub.py:121`. Re-read that assignment after the rebase.
Do not pick 14 from this worktree if `main` already took 14 or higher.

`src/brigade/fleet_hub.py` is 1929 lines and is not in
`LEGACY_OVERSIZED` (`tests/test_module_size_ratchet.py:10-25`). The
ceiling is 2000. Task 6 may add the `worklore_store.ensure_schema(conn)`
call next to `fleet_hub_preference.ensure_schema(conn)` at
`src/brigade/fleet_hub.py:528` plus the version bump at `:121`. Do not
land Worklore SQL, handlers, or validation inside `fleet_hub.py`.

## Architecture

- `worklore_validate.py` owns field bounds, lifecycle edges, burn
  eligibility, and unknown-field rejection.
- `worklore_store.py` owns the five additive SQLite tables, one-transaction
  mutations, optimistic versions, import conflict rules, and
  `PRAGMA foreign_keys=ON` on every connection it touches.
- `worklore_http.py` owns path parsing, authz, JSON envelopes, and status
  codes. Tests call it with a fake caller and an in-memory or temp SQLite
  connection. They do not start `fleet_hub.make_server` until Task 6.
- `worklore_client.py` is the only node or admin HTTP client for `/work`.
  It owns `import_batch` and `list_items`. Adapter slices import those
  names. They do not copy them.
- `cli/fleet.py` adds `brigade fleet work` and dispatches through the
  client.

## File map

| File | Responsibility |
| --- | --- |
| Create `src/brigade/worklore_validate.py` | Bounds, lifecycle edges, burn rules, forbidden import keys. |
| Create `src/brigade/worklore_store.py` | Schema, CRUD, events, links, imports, attempts, burn query. |
| Create `src/brigade/worklore_http.py` | `/work` handlers returning `(status, body)`. |
| Create `src/brigade/worklore_client.py` | Authenticated `/work` transport, including PATCH, DELETE, and import timeout. |
| Create `tests/test_worklore_validate.py` | Field and lifecycle unit tests. |
| Create `tests/test_worklore_store.py` | SQLite transaction and burn tests. |
| Create `tests/test_worklore_http.py` | Authz and JSON contract tests. |
| Create `tests/test_worklore_client.py` | Client fail-closed and CLI tests. |
| Modify `src/brigade/cli/fleet.py:45-270` | Register `fleet work` subcommands at the end of `register()`. |
| Modify `src/brigade/fleet_hub.py:121` and `:492-529` | Task 6 only: schema bump and `ensure_schema`. |
| Modify `src/brigade/fleet_hub_http.py:440-656` | Task 6 only: `/work` GET, POST, PATCH, DELETE. |
| Modify `src/brigade/fleet_client.py` | Task 6 only: re-export worklore client names. |
| Modify `tests/test_fleet_cloud.py:194-216` | Task 6 only: v4 migration still creates Worklore tables. |
| Modify `tests/test_fleet_claims.py`, `tests/test_fleet_nodes.py`, `tests/test_fleet_dashboard.py` | Task 6 only: existing routes unchanged when Worklore is off. |

## Pinned decisions

- Identifiers: `work_id` is `wl-` plus 24 lowercase hex from
  `secrets.token_hex(12)`. `link_id` is `lnk-` plus 24 hex. `event_id` is
  the client key when it matches `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`,
  otherwise `evt-` plus 24 hex. The event primary key is
  `(work_id, event_id)`.
- Create defaults: `status=captured`, `priority=normal`,
  `burn_eligible=0`, `burn_rank=1000`, `token_appetite=medium`,
  `execution_mode=manual`, `acceptance_json=[]`, `attempt_count=0`,
  `version=1`.
- Lifecycle edges: `captured` to `defining`, `ready`, or `canceled`.
  `defining` to `ready`, `deferred`, or `canceled`. `ready` to `claimed`,
  `deferred`, or `canceled`. `claimed` to `running`, `blocked`, or
  `canceled`. `running` to `verifying`, `blocked`, or `canceled`.
  `verifying` to `completed`, `blocked`, or `canceled`. `deferred` to
  `ready` or `canceled`. `blocked` to `ready` or `canceled`. `completed`
  or `canceled` to `archived`. No other edges.
- API field `acceptance` maps to `acceptance_json`. Link field
  `source_acceptance` maps to `source_acceptance_json`. Effective
  acceptance is operator `acceptance` if non-empty, else the oldest
  eligible link's `source_acceptance`.
- `ready` and the burn queue require one to twenty effective acceptance
  strings.
- Burn eligibility requires `status=ready`, `burn_eligible=1`, effective
  acceptance present, `execution_mode` in `{agent, agent-with-review}`,
  empty `blocker`, `attempt_count < 2`, `review_after` absent or in the
  past, and no link with `source_policy` other than `eligible`. Two
  failed attempts exclude the item until `attempt-reset`.
- Burn order: `burn_rank` ascending, then `spend_by` earliest with null
  last, then `ready_at` oldest, then `work_id` ascending.
- Unknown JSON fields return 400 with `code=unknown-field`.
- Missing `If-Match` on PATCH, transition, or attempt returns 400 with
  `code=if-match-required`. A stale `If-Match` returns 409 with
  `code=version-conflict`.
- Worklore mutation bodies max 262144 bytes. Import bodies max 1048576
  bytes. Both are below `fleet_hub.MAX_BODY_BYTES`.
- Dashboard cookies never authorize `/work`.
- Node tokens may GET items, GET events, GET burn, POST imports for
  adapters they own, POST execution links for their own `node_id/run_id`,
  and POST `started`/`failed` attempts for those runs. They cannot PATCH
  scheduling fields, POST transitions, or POST `reset`.
- Admin Worklore mutations succeed on a hub started without
  `--allow-admin-writes`. That flag remains scoped to `/events` and
  `/claims` (`src/brigade/fleet_hub_http.py:575-584`,
  `src/brigade/cli/fleet.py:65-72`).
- Routes stay 404 until Task 6 sets `BRIGADE_WORKLORE_ENABLED=1` or
  `[fleet.worklore] enabled = true`. Default is off.
- Stable error codes: `unauthorized`, `forbidden`, `not-found`,
  `version-conflict`, `if-match-required`, `invalid-transition`,
  `unknown-field`, `field-bound`, `acceptance-required`,
  `import-conflict`, `adapter-owner-mismatch`, `execution-mismatch`,
  `attempt-forbidden`, `hub-unavailable`.
- Errors never include SQLite text, tokens, filesystem paths, or upstream
  bodies.
- In-house Brigade commits in this plan include a coding-agent
  `Co-Authored-By` trailer. Known-good identities are in `AGENTS.md`.
  Substitute the agent that made the commit.

## Task 1: validation module

**Files:**

- Create: `src/brigade/worklore_validate.py`
- Create: `tests/test_worklore_validate.py`

- [x] Write the failing tests

```python
from brigade import worklore_validate as v


def test_create_payload_rejects_unknown_and_overlong_fields():
    parsed = v.parse_create({"title": "Rotate NAS restic password", "kind": "fleet"})
    assert parsed["title"] == "Rotate NAS restic password"
    assert parsed["kind"] == "fleet"
    assert parsed["status"] == "captured"
    assert parsed["burn_rank"] == 1000
    for raw, code in (
        ({"title": "x", "kind": "fleet", "extra": True}, "unknown-field"),
        ({"title": "", "kind": "fleet"}, "field-bound"),
        ({"title": "x" * 241, "kind": "fleet"}, "field-bound"),
        ({"title": "ok", "kind": "not-a-kind"}, "field-bound"),
        ({"title": "ok\x00", "kind": "fleet"}, "field-bound"),
    ):
        with pytest.raises(v.WorkloreValidationError) as excinfo:
            v.parse_create(raw)
        assert excinfo.value.code == code


def test_ready_requires_acceptance_and_burn_rules_match_spec():
    item = {
        "status": "ready",
        "burn_eligible": 1,
        "acceptance": ["restic snapshots --latest 1 succeeds"],
        "execution_mode": "agent",
        "blocker": None,
        "attempt_count": 1,
        "review_after": None,
        "source_policies": ["eligible"],
    }
    assert v.burn_eligible(item) is True
    item["attempt_count"] = 2
    assert v.burn_eligible(item) is False
    item["attempt_count"] = 1
    item["source_policies"] = ["label-removed"]
    assert v.burn_eligible(item) is False
    item["source_policies"] = ["eligible"]
    item["acceptance"] = []
    with pytest.raises(v.WorkloreValidationError) as excinfo:
        v.assert_ready(item)
    assert excinfo.value.code == "acceptance-required"


def test_lifecycle_edges_are_closed():
    assert v.can_transition("captured", "ready") is True
    assert v.can_transition("ready", "claimed") is True
    assert v.can_transition("ready", "running") is False
    assert v.can_transition("completed", "archived") is True
    assert v.can_transition("archived", "ready") is False
```

- [x] Run red through Brigade:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_worklore_validate.py"]' --capture brigade-work
```

Expect FAIL: `ModuleNotFoundError: No module named 'brigade.worklore_validate'`.

- [x] Implement the minimal module. Reject unknown keys with
  `WorkloreValidationError("unknown field: {name}", code="unknown-field")`.
  Enforce title 1-240, description max 8000, scope max 128, blocker max
  2000, burn_rank 0-10000, kinds
  `{repo,fleet,research,writing,admin,experiment,idea,other}`, priorities
  `{low,normal,high,urgent}`, token appetites
  `{small,medium,large,max}`, execution modes
  `{manual,agent,agent-with-review}`. Reject C0/C1 controls with the same
  helper shape as `fleet_hub._contains_controls` at
  `src/brigade/fleet_hub.py:389`. `assert_ready` requires one to twenty
  non-empty acceptance strings of at most 400 characters.
  `burn_eligible(item)` reads `item["acceptance"]` as the already-resolved
  effective list and `item.get("source_policies") or []` as the link
  policies.

- [x] Run green through Brigade with the same command. Expect `passed`.

- [x] Commit:

```bash
git add src/brigade/worklore_validate.py tests/test_worklore_validate.py
git commit -m "$(cat <<'EOF'
feat: validate Worklore fields and lifecycle edges

EOF
)"
```

Use the trailer identity of the agent that made the commit.

## Task 2: store, events, and burn selection

**Files:**

- Create: `src/brigade/worklore_store.py`
- Create: `tests/test_worklore_store.py`

- [x] Write the failing tests

```python
import sqlite3

from brigade import fleet_hub
from brigade import worklore_store as store


def _conn(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "worklore.db")
    store.ensure_schema(conn)
    return conn


def test_create_and_transition_share_one_transaction(tmp_path):
    conn = _conn(tmp_path)
    item = store.create_item(conn, {"title": "Fleet maintenance", "kind": "fleet"}, actor_id="admin")
    assert item["work_id"].startswith("wl-")
    assert item["status"] == "captured"
    assert item["version"] == 1
    events = store.list_events(conn, item["work_id"])
    assert [event["event_type"] for event in events] == ["created"]
    updated = store.transition(
        conn,
        item["work_id"],
        to_status="defining",
        expected_version=1,
        actor_type="operator",
        actor_id="admin",
    )
    assert updated["status"] == "defining" and updated["version"] == 2
    assert [event["event_type"] for event in store.list_events(conn, item["work_id"])] == [
        "created",
        "transitioned",
    ]


def test_ready_sets_ready_at_and_burn_order_is_stable(tmp_path):
    conn = _conn(tmp_path)
    first = store.create_item(conn, {"title": "A", "kind": "fleet"}, actor_id="admin")
    second = store.create_item(conn, {"title": "B", "kind": "fleet"}, actor_id="admin")
    for work_id, rank, spend_by in (
        (second["work_id"], 10, "2026-09-01T00:00:00+00:00"),
        (first["work_id"], 10, "2026-08-30T00:00:00+00:00"),
    ):
        store.patch_item(
            conn,
            work_id,
            {
                "acceptance": ["done"],
                "burn_eligible": True,
                "execution_mode": "agent",
                "burn_rank": rank,
                "spend_by": spend_by,
            },
            expected_version=1,
            actor_id="admin",
        )
        store.transition(conn, work_id, to_status="ready", expected_version=2, actor_id="admin")
    queue = store.burn_queue(conn)
    assert [item["title"] for item in queue["items"]] == ["A", "B"]
    assert queue["exclusions"]["attempt-limit"] == 0
    assert queue["exclusions"]["source-policy"] == 0


def test_exclusion_bucket_is_first_match(tmp_path):
    conn = _conn(tmp_path)
    store.create_item(conn, {"title": "Not ready", "kind": "fleet"}, actor_id="admin")
    queue = store.burn_queue(conn)
    assert queue["exclusions"]["not-ready"] == 1
    assert queue["exclusions"]["acceptance-required"] == 0


def test_work_links_require_existing_work_id(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO work_links (link_id, work_id, link_type, external_key, "
            "source_policy, source_acceptance_json, synced_at) VALUES "
            "('lnk-missing', 'wl-missing', 'url', 'https://example.com/a', "
            "'eligible', '[]', '2026-08-29T00:00:00+00:00')"
        )
        conn.commit()


def test_stale_version_does_not_write_an_event(tmp_path):
    conn = _conn(tmp_path)
    item = store.create_item(conn, {"title": "Conflict", "kind": "admin"}, actor_id="admin")
    with pytest.raises(store.WorkloreConflict) as excinfo:
        store.patch_item(conn, item["work_id"], {"priority": "high"}, expected_version=0, actor_id="admin")
    assert excinfo.value.code == "version-conflict"
    assert store.list_events(conn, item["work_id"])[0]["event_type"] == "created"
    assert store.get_item(conn, item["work_id"])["version"] == 1
```

- [x] Run red through Brigade:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_worklore_store.py"]' --capture brigade-work
```

Expect FAIL: `ModuleNotFoundError: No module named 'brigade.worklore_store'`.

- [x] Implement `ensure_schema` with the five tables and indexes from the
  spec: `work_items_status`, `work_items_burn`, `work_items_ready_at`,
  `work_events_work_id`, `work_links_external`. Use
  `FOREIGN KEY (work_id) REFERENCES work_items(work_id)` with delete
  restricted. `UNIQUE(link_type, external_key)` on `work_links`. Event
  primary key is `(work_id, event_id)`. `ensure_schema` and every public
  store function issue `PRAGMA foreign_keys=ON` on the connection before
  any `BEGIN IMMEDIATE`. SQLite ignores that pragma inside a transaction.
  Do not set that pragma in the test helper. `create_item`, `patch_item`,
  `transition`, and `record_attempt` each run `BEGIN IMMEDIATE`, write
  the projection and the event, then `COMMIT`. On validation or version
  failure, `ROLLBACK`. `burn_queue` returns
  `{"items": [...], "exclusions": {"acceptance-required": N, "not-ready": N, "not-eligible": N, "manual-mode": N, "blocker": N, "attempt-limit": N, "review-after": N, "source-policy": N}}`
  and counts each excluded item in exactly one bucket using the first-match
  order in `docs/phase-worklore-ledger.md`.

- [x] Run green through Brigade with the same command. Expect `passed`.

- [x] Commit:

```bash
git add src/brigade/worklore_store.py tests/test_worklore_store.py
git commit -m "$(cat <<'EOF'
feat: store Worklore items and append-only events

EOF
)"
```

## Task 3: links, imports, attempts, and execution joins

**Files:**

- Modify: `src/brigade/worklore_store.py`
- Modify: `tests/test_worklore_store.py`

- [x] Write the failing tests

```python
def test_import_batch_is_idempotent_and_keeps_scheduling(tmp_path):
    conn = _conn(tmp_path)
    observation = {
        "external_key": "escoffier-labs/brigade#1",
        "link_type": "github",
        "title": "Burn queue item",
        "description": "Do the work",
        "acceptance": ["tests pass"],
        "external_state": "open",
        "external_updated_at": "2026-08-29T00:00:00+00:00",
        "url": "https://github.com/escoffier-labs/brigade/issues/1",
        "display_ref": "escoffier-labs/brigade#1",
        "source_policy": "eligible",
    }
    first = store.import_batch(
        conn,
        adapter_id="github:escoffier-labs",
        source_type="github",
        idempotency_key="batch-1",
        observations=[observation],
        actor_id="node-a",
        owner_node="node-a",
    )
    store.patch_item(
        conn,
        first["items"][0]["work_id"],
        {"burn_rank": 5, "token_appetite": "large", "acceptance": ["operator override"]},
        expected_version=1,
        actor_id="admin",
    )
    conn.execute(
        "UPDATE work_sync_cursors SET last_success_at = '2026-01-01T00:00:00+00:00', "
        "updated_at = '2026-01-01T00:00:00+00:00' WHERE adapter_id = ?",
        ("github:escoffier-labs",),
    )
    conn.commit()
    second = store.import_batch(
        conn,
        adapter_id="github:escoffier-labs",
        source_type="github",
        idempotency_key="batch-1",
        observations=[observation],
        actor_id="node-a",
        owner_node="node-a",
    )
    replay_cursor = conn.execute(
        "SELECT last_success_at, updated_at FROM work_sync_cursors WHERE adapter_id = ?",
        ("github:escoffier-labs",),
    ).fetchone()
    assert replay_cursor[0] > "2026-01-01T00:00:00+00:00"
    assert replay_cursor[1] > "2026-01-01T00:00:00+00:00"
    later = dict(observation)
    later["acceptance"] = ["source refreshed"]
    later["external_updated_at"] = "2026-08-30T00:00:00+00:00"
    store.import_batch(
        conn,
        adapter_id="github:escoffier-labs",
        source_type="github",
        idempotency_key="batch-1b",
        observations=[later],
        actor_id="node-a",
        owner_node="node-a",
    )
    replay = store.import_batch(
        conn,
        adapter_id="github:escoffier-labs",
        source_type="github",
        idempotency_key="batch-1",
        observations=[observation],
        actor_id="node-a",
        owner_node="node-a",
    )
    item = store.get_item(conn, first["items"][0]["work_id"])
    links = store.list_links(conn, item["work_id"])
    assert second["created"] == 1 and second["updated"] == 0
    assert replay["created"] == second["created"]
    assert replay["updated"] == 0
    assert [entry["external_key"] for entry in replay["items"]] == [observation["external_key"]]
    assert item["burn_rank"] == 5
    assert item["token_appetite"] == "large"
    assert item["acceptance"] == ["operator override"]
    assert item["effective_acceptance"] == ["operator override"]
    assert links[0]["source_acceptance"] == ["source refreshed"]
    types = [event["event_type"] for event in store.list_events(conn, item["work_id"])]
    assert types.count("sync-observed") == 2


def test_import_rejects_secrets_paths_duplicate_keys_and_foreign_nodes(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(store.WorkloreValidationError) as excinfo:
        store.import_batch(
            conn,
            adapter_id="github:escoffier-labs",
            source_type="github",
            idempotency_key="batch-bad",
            observations=[{"external_key": "a/b#1", "link_type": "github", "title": "x", "source_policy": "eligible", "prompt": "secret"}],
            actor_id="node-a",
            owner_node="node-a",
        )
    assert excinfo.value.code == "unknown-field"
    store.import_batch(
        conn,
        adapter_id="github:escoffier-labs",
        source_type="github",
        idempotency_key="batch-2",
        observations=[{
            "external_key": "a/b#2",
            "link_type": "github",
            "title": "One",
            "url": "https://github.com/a/b/issues/2",
            "external_state": "open",
            "source_policy": "eligible",
        }],
        actor_id="node-a",
        owner_node="node-a",
    )
    with pytest.raises(store.WorkloreConflict) as excinfo:
        store.import_batch(
            conn,
            adapter_id="github:other",
            source_type="github",
            idempotency_key="batch-3",
            observations=[{
                "external_key": "a/b#2",
                "link_type": "github",
                "title": "Two",
                "url": "https://github.com/a/b/issues/2",
                "external_state": "open",
                "source_policy": "eligible",
            }],
            actor_id="node-b",
            owner_node="node-b",
        )
    assert excinfo.value.code == "import-conflict"
    with pytest.raises(store.WorkloreForbidden) as excinfo:
        store.import_batch(
            conn,
            adapter_id="github:escoffier-labs",
            source_type="github",
            idempotency_key="batch-4",
            observations=[{
                "external_key": "a/b#3",
                "link_type": "github",
                "title": "Three",
                "url": "https://github.com/a/b/issues/3",
                "external_state": "open",
                "source_policy": "eligible",
            }],
            actor_id="node-b",
            owner_node="node-b",
        )
    assert excinfo.value.code == "adapter-owner-mismatch"


def test_execution_link_requires_existing_run_for_caller_node(tmp_path):
    conn = _conn(tmp_path)
    item = store.create_item(conn, {"title": "Join run", "kind": "fleet"}, actor_id="admin")
    conn.execute(
        "INSERT INTO events (node_id, run_id, sequence, digest, state, ts, received_at) "
        "VALUES ('node-a', 'run-1', 1, 'd', 'run.created', 't', 't')"
    )
    conn.commit()
    linked = store.link_execution(conn, item["work_id"], node_id="node-a", run_id="run-1", actor_id="node-a")
    assert linked["link_type"] == "fleet-run"
    assert linked["external_key"] == "node-a/run-1"
    with pytest.raises(store.WorkloreForbidden) as excinfo:
        store.link_execution(conn, item["work_id"], node_id="node-b", run_id="run-1", actor_id="node-a")
    assert excinfo.value.code == "execution-mismatch"


def test_record_attempt_failed_excludes_after_two(tmp_path):
    conn = _conn(tmp_path)
    item = store.create_item(conn, {"title": "Burn me", "kind": "fleet"}, actor_id="admin")
    store.patch_item(
        conn,
        item["work_id"],
        {"acceptance": ["done"], "burn_eligible": True, "execution_mode": "agent"},
        expected_version=1,
        actor_id="admin",
    )
    store.transition(conn, item["work_id"], to_status="ready", expected_version=2, actor_id="admin")
    store.record_attempt(conn, item["work_id"], action="failed", expected_version=3, actor_id="admin")
    store.record_attempt(conn, item["work_id"], action="failed", expected_version=4, actor_id="admin")
    queue = store.burn_queue(conn)
    assert queue["items"] == []
    assert queue["exclusions"]["attempt-limit"] == 1
    reset = store.record_attempt(conn, item["work_id"], action="reset", expected_version=5, actor_id="admin")
    assert reset["attempt_count"] == 0


def test_record_attempt_started_bumps_version_without_count(tmp_path):
    conn = _conn(tmp_path)
    item = store.create_item(conn, {"title": "Start me", "kind": "fleet"}, actor_id="admin")
    conn.execute(
        "INSERT INTO events (node_id, run_id, sequence, digest, state, ts, received_at) "
        "VALUES ('node-a', 'run-1', 1, 'd', 'run.created', 't', 't')"
    )
    conn.commit()
    store.link_execution(conn, item["work_id"], node_id="node-a", run_id="run-1", actor_id="node-a")
    started = store.record_attempt(
        conn,
        item["work_id"],
        action="started",
        expected_version=1,
        actor_id="node-a",
        node_id="node-a",
        run_id="run-1",
    )
    assert started["attempt_count"] == 0
    assert started["version"] == 2


def test_node_failed_without_owned_fleet_run_is_forbidden(tmp_path):
    conn = _conn(tmp_path)
    item = store.create_item(conn, {"title": "No run", "kind": "fleet"}, actor_id="admin")
    with pytest.raises(store.WorkloreForbidden) as excinfo:
        store.record_attempt(
            conn,
            item["work_id"],
            action="failed",
            expected_version=1,
            actor_id="node-a",
            node_id="node-a",
            run_id="run-missing",
        )
    assert excinfo.value.code == "attempt-forbidden"
```

- [x] Run red through Brigade with `tests/test_worklore_store.py`. Expect
  FAIL on the new test names (`import_batch` / `link_execution` /
  `record_attempt` missing).

- [x] Implement `import_batch`, `link_execution`, `list_items`,
  `list_links`, `list_all_events`, and `record_attempt`. First
  observation of an external key creates a native-shaped item with
  default scheduling and a `github` or `brigade` link. Later observations
  update only `title`, `external_state`, `external_updated_at`,
  `synced_at`, `stale_at`, `source_policy`, and `source_acceptance`.
  They never write `burn_*`, `token_appetite`, `execution_mode`,
  Worklore `priority`, operator `acceptance`, or `status`.   Persist
  `(adapter_id, idempotency_key, fingerprint)` in `work_import_keys`.
  Identical key plus fingerprint writes no observations or events, even
  after a newer batch, and still updates `work_sync_cursors.last_success_at`
  and `updated_at`. `record_attempt(conn, work_id, *, action,
  expected_version, actor_id, node_id=None, run_id=None)` treats
  `node_id` as the authenticated caller. A node `started` or `failed`
  requires `run_id` naming a `fleet-run` link with
  `external_key="{node_id}/{run_id}"`. Otherwise raise
  `WorkloreForbidden` with `attempt-forbidden`. `started` increments
  `version` and leaves `attempt_count` unchanged.
  `list_items(conn, source="github")` returns items whose `links` include
  that `link_type`. `link_execution` checks Fleet Hub `events` for
  `(node_id, run_id)` and rejects another node's run when the actor is a
  node. Missing source rows set `stale_at` and append `sync-stale`.

- [x] Run green through Brigade. Expect `passed`.

- [x] Commit:

```bash
git add src/brigade/worklore_store.py tests/test_worklore_store.py
git commit -m "$(cat <<'EOF'
feat: import Worklore observations without clobbering schedule fields

EOF
)"
```

## Task 4: HTTP handlers without hub routing

**Files:**

- Create: `src/brigade/worklore_http.py`
- Create: `tests/test_worklore_http.py`

- [x] Write the failing tests. Call `worklore_http.handle(conn, request)`
  with a dataclass, not `make_server`.

```python
from brigade import fleet_hub
from brigade import worklore_http as http
from brigade import worklore_store as store


def _req(method, path, *, admin=False, node=None, body=None, headers=None):
    return http.Request(
        method=method,
        path=path,
        is_admin=admin,
        node_id=node,
        body=body or {},
        headers=headers or {},
    )


def _http_conn(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "db")
    store.ensure_schema(conn)
    return conn


def test_cookie_caller_cannot_read_or_write(tmp_path):
    conn = _http_conn(tmp_path)
    status, body = http.handle(conn, _req("GET", "/work/items"))
    assert status == 401 and body["code"] == "unauthorized"


def test_node_can_read_and_import_but_cannot_transition(tmp_path):
    conn = _http_conn(tmp_path)
    status, created = http.handle(
        conn,
        _req("POST", "/work/items", admin=True, body={"title": "Native", "kind": "fleet"}),
    )
    assert status == 201
    work_id = created["item"]["work_id"]
    status, body = http.handle(conn, _req("GET", f"/work/items/{work_id}", node="node-a"))
    assert status == 200 and body["item"]["title"] == "Native"
    status, body = http.handle(
        conn,
        _req(
            "POST",
            f"/work/items/{work_id}/transitions",
            node="node-a",
            body={"to_status": "defining"},
            headers={"If-Match": "1"},
        ),
    )
    assert status == 403 and body["code"] == "forbidden"


def test_patch_and_transition_require_if_match(tmp_path):
    conn = _http_conn(tmp_path)
    _, created = http.handle(
        conn,
        _req("POST", "/work/items", admin=True, body={"title": "Edit me", "kind": "admin"}),
    )
    work_id = created["item"]["work_id"]
    status, body = http.handle(
        conn,
        _req("PATCH", f"/work/items/{work_id}", admin=True, body={"priority": "high"}),
    )
    assert status == 400 and body["code"] == "if-match-required"
    status, body = http.handle(
        conn,
        _req(
            "POST",
            f"/work/items/{work_id}/transitions",
            admin=True,
            body={"to_status": "defining"},
        ),
    )
    assert status == 400 and body["code"] == "if-match-required"
    status, ok = http.handle(
        conn,
        _req(
            "PATCH",
            f"/work/items/{work_id}",
            admin=True,
            body={"priority": "high"},
            headers={"If-Match": "1"},
        ),
    )
    assert status == 200 and ok["item"]["version"] == 2
    status, ready = http.handle(
        conn,
        _req(
            "POST",
            f"/work/items/{work_id}/transitions",
            admin=True,
            body={"to_status": "defining"},
            headers={"If-Match": "2"},
        ),
    )
    assert status == 200 and ready["item"]["status"] == "defining"
    status, conflict = http.handle(
        conn,
        _req(
            "PATCH",
            f"/work/items/{work_id}",
            admin=True,
            body={"priority": "low"},
            headers={"If-Match": "2"},
        ),
    )
    assert status == 409 and conflict["code"] == "version-conflict"


def test_invalid_transition_and_missing_item_use_stable_codes(tmp_path):
    conn = _http_conn(tmp_path)
    _, created = http.handle(
        conn,
        _req("POST", "/work/items", admin=True, body={"title": "X", "kind": "fleet"}),
    )
    work_id = created["item"]["work_id"]
    status, body = http.handle(
        conn,
        _req(
            "POST",
            f"/work/items/{work_id}/transitions",
            admin=True,
            body={"to_status": "running"},
            headers={"If-Match": "1"},
        ),
    )
    assert status == 400 and body["code"] == "invalid-transition"
    status, body = http.handle(conn, _req("GET", "/work/items/wl-missing", admin=True))
    assert status == 404 and body["code"] == "not-found"


def test_attempts_body_is_action_and_optional_run_id(tmp_path):
    conn = _http_conn(tmp_path)
    _, created = http.handle(
        conn,
        _req("POST", "/work/items", admin=True, body={"title": "Try", "kind": "fleet"}),
    )
    work_id = created["item"]["work_id"]
    status, body = http.handle(
        conn,
        _req(
            "POST",
            f"/work/items/{work_id}/attempts",
            node="node-a",
            body={"action": "failed"},
            headers={"If-Match": "1"},
        ),
    )
    assert status == 403 and body["code"] == "attempt-forbidden"
```

- [x] Run red through Brigade:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_worklore_http.py"]' --capture brigade-work
```

Expect FAIL: `ModuleNotFoundError: No module named 'brigade.worklore_http'`.

- [x] Implement `handle`. Supported paths: `GET /work/items`,
  `GET /work/items/{work_id}`, `GET /work/items/{work_id}/events`,
  `GET /work/events`, `GET /work/queue/burn`, `POST /work/items`,
  `PATCH /work/items/{work_id}`, `POST /work/items/{work_id}/transitions`,
  `POST /work/items/{work_id}/attempts`,
  `POST /work/items/{work_id}/links`,
  `DELETE /work/items/{work_id}/links/{link_id}`, `POST /work/imports`,
  `POST /work/items/{work_id}/execution`. Attempts body is
  `{ "action", "run_id"? }`. Pass authenticated `node_id` and optional
  `run_id` into `record_attempt`. Missing admin or node identity
  is 401. Cookie-only callers never reach this function. Map store
  exceptions to the stable codes. Do not put SQLite errors in `error`.

- [x] Run green through Brigade. Expect `passed`.

- [x] Commit:

```bash
git add src/brigade/worklore_http.py tests/test_worklore_http.py
git commit -m "$(cat <<'EOF'
feat: add isolated Worklore HTTP handlers

EOF
)"
```

## Task 5: client and CLI

**Files:**

- Create: `src/brigade/worklore_client.py`
- Create: `tests/test_worklore_client.py`
- Modify: `src/brigade/cli/fleet.py:45-270` to register commands after
  the preference parsers. Add `_dispatch_work_*` functions after
  `register()`.

- [x] Write the failing tests

```python
from brigade import worklore_client
from brigade.cli import fleet as fleet_cli


def test_client_fails_closed_when_hub_is_configured_and_down(monkeypatch):
    monkeypatch.setattr(
        worklore_client,
        "load_fleet_settings",
        lambda: {"hub_url": "http://127.0.0.1:9", "admin_token": "admin", "node_token": "node"},  # content-guard: allow loopback-ipv4
    )
    with pytest.raises(worklore_client.FleetClientError, match="hub-unavailable"):
        worklore_client.create_item({"title": "Native", "kind": "fleet"})


def test_fleet_work_create_and_burn_json(monkeypatch, capsys):
    monkeypatch.setattr(
        worklore_client,
        "create_item",
        lambda body: {"item": {"work_id": "wl-aaa", "title": body["title"], "status": "captured"}},
    )
    monkeypatch.setattr(
        worklore_client,
        "burn_queue",
        lambda: {"items": [], "exclusions": {"acceptance-required": 1, "source-policy": 0}},
    )
    assert fleet_cli._dispatch_work_create(argparse.Namespace(title="X", kind="fleet", json=True)) == 0
    assert '"wl-aaa"' in capsys.readouterr().out
    assert fleet_cli._dispatch_work_burn(argparse.Namespace(json=True)) == 0
    assert "acceptance-required" in capsys.readouterr().out
```

- [x] Run red through Brigade:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_worklore_client.py"]' --capture brigade-work
```

Expect FAIL: `ModuleNotFoundError: No module named 'brigade.worklore_client'`.

- [x] Implement `worklore_client.py` with its own transport. Do not call
  `fleet_client._admin_request` (`src/brigade/fleet_client.py:1384-1415`).
  That helper emits only GET or POST, uses
  `REPORT_TIMEOUT_SECONDS = 2.0` (`src/brigade/fleet_client.py:117`), and
  cannot carry `If-Match`. Worklore GET/POST mutations use a 5.0s
  timeout. PATCH and DELETE use the same 5.0s timeout and send
  `If-Match`. `import_batch` uses `IMPORT_TIMEOUT_SECONDS = 30.0`.
  `load_fleet_settings` still returns exactly `hub_url` / `admin_token` /
  `node_token` (`src/brigade/fleet_client.py:200-226`). Normal create,
  patch, transition, attempt reset, and operator-link mutations use the
  enrolled node token plus `Idempotency-Key` on native create. The Hub
  admin token remains a server-side override and is not used by the CLI
  client. Node imports, execution links, and started/failed attempts
  continue to use the node token. A configured hub that raises
  `OSError` or `TimeoutError` becomes `FleetClientError("hub-unavailable")`.
- [x] Export `create_item`, `get_item`, `list_items`, `list_events`,
  `list_all_events`, `burn_queue`, `patch_item`, `transition`,
  `record_attempt`, `add_link`, `delete_link`, `import_batch`, and
  `link_execution`. `import_batch(payload)` takes one dict with keys
  `adapter_id`, `source_type`, `idempotency_key`, and `observations`.
  It does not take those values as separate keyword arguments.
- [x] Add
  `brigade fleet work create|show|list|burn|patch|transition|attempt|link|unlink`
  to `register()` after the preference parsers at
  `src/brigade/cli/fleet.py:251-270`. Dispatchers print JSON when
  `--json` is set and never print tokens. `list` accepts `--source`.

- [x] Run green through Brigade. Expect `passed`.

- [x] Commit:

```bash
git add src/brigade/worklore_client.py src/brigade/cli/fleet.py tests/test_worklore_client.py
git commit -m "$(cat <<'EOF'
feat: add brigade fleet work client and CLI

EOF
)"
```

## Task 6: Fleet Hub integration after the precondition passes

**Do not start this task until the three integration precondition checks
above are true.**

**Files:**

- Modify: `src/brigade/fleet_hub.py:121` (`SCHEMA_VERSION`)
- Modify: `src/brigade/fleet_hub.py:492-529` (`_apply_schema`)
- Modify: `src/brigade/fleet_hub_http.py:440-656` (`do_GET`, `do_POST`, add
  `do_PATCH`, `do_DELETE`)
- Modify: `src/brigade/worklore_http.py` to add the fail-closed feature gate
- Modify: `src/brigade/fleet_client.py` re-exports
- Modify: `tests/test_fleet_cloud.py:194-216`
- Modify: `tests/test_worklore_http.py` to add live-server tests
- Modify: existing fleet HTTP tests only to prove `/work` is 404 when
  disabled

- [x] Fetch and rebase onto current `origin/main`. Confirm
  `git merge-base --is-ancestor origin/main HEAD`. Run
  `brigade fleet claims --json` and confirm no live conflicting owner.
  Record the operator confirmation that the warned concurrent Fleet Hub
  work has landed. Read the new `SCHEMA_VERSION`. Set
  `NEXT = that number + 1`.

- [x] Write the failing integration tests

```python
def test_worklore_enabled_is_fail_closed_and_reads_fleet_config(tmp_path):
    config = tmp_path / "fleet.toml"
    assert worklore_http.enabled(environ={}, config_path=config) is False
    config.write_text("[fleet.worklore]\nenabled = true\n")
    assert worklore_http.enabled(environ={}, config_path=config) is True
    assert worklore_http.enabled(
        environ={"BRIGADE_WORKLORE_ENABLED": "0"}, config_path=config
    ) is False
    assert worklore_http.enabled(
        environ={"BRIGADE_WORKLORE_ENABLED": "1"}, config_path=config
    ) is True


def test_worklore_routes_are_absent_when_disabled(tmp_path):
    with _hub(tmp_path) as hub:
        status, body = _request(hub, "GET", "/work/items", token=ADMIN_TOKEN)
        assert status == 404
        assert body["error"] == "not found"


def test_worklore_native_create_to_burn_on_enabled_hub(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGADE_WORKLORE_ENABLED", "1")
    with _hub(tmp_path) as hub:
        status, created = _request(
            hub, "POST", "/work/items", token=ADMIN_TOKEN, body={"title": "NAS restic", "kind": "fleet"}
        )
        assert status == 201
        work_id = created["item"]["work_id"]
        _request(
            hub,
            "PATCH",
            f"/work/items/{work_id}",
            token=ADMIN_TOKEN,
            body={
                "acceptance": ["restic snapshots --latest 1 succeeds"],
                "burn_eligible": True,
                "execution_mode": "agent",
            },
            extra_headers={"If-Match": "1"},
        )
        _request(
            hub,
            "POST",
            f"/work/items/{work_id}/transitions",
            token=ADMIN_TOKEN,
            body={"to_status": "ready"},
            extra_headers={"If-Match": "2"},
        )
        status, queue = _request(hub, "GET", "/work/queue/burn", token=ADMIN_TOKEN)
        assert status == 200
        assert queue["items"][0]["work_id"] == work_id


def test_admin_worklore_patch_succeeds_without_allow_admin_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGADE_WORKLORE_ENABLED", "1")
    with _hub(tmp_path) as hub:
        _, created = _request(
            hub, "POST", "/work/items", token=ADMIN_TOKEN, body={"title": "Admin write", "kind": "admin"}
        )
        status, patched = _request(
            hub,
            "PATCH",
            f"/work/items/{created['item']['work_id']}",
            token=ADMIN_TOKEN,
            body={"priority": "high"},
            extra_headers={"If-Match": "1"},
        )
        assert status == 200
        assert patched["item"]["priority"] == "high"


def test_existing_claim_and_dashboard_routes_stay_unchanged(tmp_path):
    with _hub(tmp_path) as hub:
        assert _request(hub, "GET", "/claims", token=ADMIN_TOKEN)[0] == 200
        assert _request(hub, "GET", "/health")[0] == 200
```

Reuse `_hub` and `_request` from `tests/test_fleet_cloud.py:280-307`.
`_hub` already calls `make_server(..., allow_admin_writes=False)` because
that keyword defaults to false at `src/brigade/fleet_hub.py:1851`.
Extend `_request` with `extra_headers` rather than copying the helper.

- [x] Run red through Brigade:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_worklore_http.py","tests/test_fleet_cloud.py","tests/test_fleet_claims.py","tests/test_fleet_nodes.py","tests/test_fleet_dashboard.py"]' --capture brigade-work
```

Expect FAIL on the new `/work` assertions until routing exists.

- [x] In `_apply_schema`, call `worklore_store.ensure_schema(conn)` next
  to `fleet_hub_preference.ensure_schema(conn)` at
  `src/brigade/fleet_hub.py:528`. Bump `SCHEMA_VERSION` to `NEXT`. Keep
  `_refuse_newer_schema` fail-closed.
- [x] In `fleet_hub_http.py`, if
  `worklore_http.enabled()` is false, leave `/work` as 404. If true, open
  the request DB the same way `/claims` does, resolve `_caller`, and
  delegate to `worklore_http.handle`. Add `do_PATCH` and `do_DELETE` that
  only accept `/work/` paths. Dashboard cookie authorization must not
  reach `handle`.
- [x] Implement `worklore_http.enabled(environ=None, config_path=None)`.
  `BRIGADE_WORKLORE_ENABLED` accepts `1`, `true`, `yes`, or `on` and
  `0`, `false`, `no`, or `off`; a recognized environment value wins.
  Otherwise read `[fleet.worklore] enabled` from
  `config_path` or `~/.brigade/fleet.toml` using `toml_compat`. Missing,
  malformed, or non-boolean configuration returns false. The default is
  off.
- [x] Update `test_v4_migration_preserves_existing_rows` so the migrated
  database also has empty `work_items`.
- [x] Re-export `create_item`, `list_items`, `burn_queue`,
  `import_batch`, `record_attempt`, and `link_execution` from
  `fleet_client.py` the way cloud helpers are re-exported.

- [x] Run the same focused gate green. Expect `passed`.
- [x] Run the broader fleet regression:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_worklore_validate.py","tests/test_worklore_store.py","tests/test_worklore_http.py","tests/test_worklore_client.py","tests/test_fleet_cloud.py","tests/test_fleet_claims.py","tests/test_fleet_nodes.py","tests/test_fleet_dashboard.py","tests/test_fleet_sync.py"]' --capture brigade-work
```

Expect `passed`.

- [x] Commit:

```bash
git add src/brigade/fleet_hub.py src/brigade/fleet_hub_http.py src/brigade/fleet_client.py src/brigade/worklore_http.py tests/test_worklore_http.py tests/test_fleet_cloud.py
git commit -m "$(cat <<'EOF'
feat: expose Worklore routes on Fleet Hub after the integration precondition

EOF
)"
```

## Verification

Every task runs `./scripts/verify-focused` through
`brigade work verify run` with `--capture brigade-work`. Task 6 also
re-runs the listed fleet suites. Do not claim success from a raw pytest
invocation.
