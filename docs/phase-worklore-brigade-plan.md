# Worklore Brigade adapter implementation plan

## Goal

Read explicitly configured local Brigade task ledgers through the public
`brigade work tasks` command and import them so the same task observed
from two nodes resolves to one external link. Execute the tasks in order
and check every box.

Approved design: `docs/phase-worklore-ledger.md`.

This plan is independently executable for normalize and public-command
reads. Tasks 3 and 4 import `worklore_client.import_batch` from the core
slice. Do not add that function here. Start Task 3 only after core Task 5
is on the branch. Tests seed a temp `.brigade/work/tasks.json` and call
the public command, then post to a fake `import_batch`. They do not edit
Fleet Hub tables, the GitHub adapter, or Ops Deck.

## Architecture

- `worklore_brigade.py` turns one public task JSON object into an
  observation. It never includes `tasks_path`, absolute paths, raw
  receipt bodies, or prompt text.
- `worklore_brigade_sync.py` walks only the configured target list, runs
  `brigade work tasks --target {path} --all --json`, writes a retry
  receipt, and posts one idempotent batch per identity.
- Canonical identity is the configured `identity` string plus `#` plus
  the ledger task id. Two nodes that share `identity=escoffier-labs/brigade`
  and task `abc123` produce `escoffier-labs/brigade#abc123`.

## File map

| File | Responsibility |
| --- | --- |
| Create `src/brigade/worklore_brigade.py` | Task JSON to observation mapping. |
| Create `src/brigade/worklore_brigade_sync.py` | Explicit targets, public command, retry receipt. |
| Create `tests/test_worklore_brigade.py` | Normalizer, path stripping, two-node dedupe, CLI. |
| Modify `src/brigade/cli/fleet.py` | `brigade fleet work sync-brigade`. |

Do not modify `worklore_client.py`.

## Pinned decisions

- Config lives in `~/.brigade/fleet.toml`:

```toml
[fleet.worklore.brigade]
targets = [
  { path = "/var/lib/brigade/targets/brigade", identity = "escoffier-labs/brigade" },
]
```

  `path` is a local directory the node can read. `identity` is
  `owner/repository` or a stable workspace slug matching
  `^[A-Za-z0-9][A-Za-z0-9._/-]{1,127}$`. Missing `targets` is a CLI
  error. The adapter never searches `$HOME` for `.brigade` folders.
- Read path is the public command
  `brigade work tasks --target {path} --all --json`. That command already
  prints full task objects, edges, and `tasks_path` from
  `src/brigade/work_cmd/session/task_ops.py:27-55`. Public edges are
  normalized as `source` / `target` / `type`
  (`src/brigade/work_cmd/edges.py:126-155`, written at `:142-146`). Use
  that output. Do not import `work_cmd.ledger._read_task_ledger` from the
  adapter.
- Drop `tasks_path` before any observation is built. Drop any string
  value that starts with `/` or matches `^[A-Za-z]:\\`.
- Observation fields follow the ledger import schema:
  `external_key`, `link_type=brigade`, `title` from `task.text` (max 240),
  `description` empty, `acceptance` from `task.acceptance` (stored as
  source acceptance), `external_state` from `task.status`, `priority` as
  source metadata only (Worklore scheduling priority is not overwritten),
  `dependencies` as `{type,id}` pairs from edges where
  `source == task_id`, `evidence_refs` as opaque ids from
  `metadata.verify_run_id` or `metadata.receipt_id` when those values
  match `^[A-Za-z0-9._-]{1,128}$`.
- Completed Brigade status `done` sends `source_policy=completed` and
  `proposed_status=completed`. Terminal `cancelled` and `dismissed` tasks send
  `source_policy=completed` without `proposed_status`, so they leave the burn
  queue without being represented as successful completion. The adapter never
  POSTs a transition.
- Adapter id is `brigade:{identity}`.
- Retry receipt path is
  `$BRIGADE_HOME/worklore/brigade/{safe_identity}.json` with mode 0600.
  `safe_identity` replaces `:`, `/`, `\`, `<`, `>`, `"`, `|`, `?`, and
  `*` with `-`. Identity `escoffier-labs/brigade` becomes
  `escoffier-labs-brigade.json`. Same fields as the GitHub receipt,
  including `skipped`. No task text.
- Batch size is at most 500. One logical identity group is built per
  `sync_brigade` invocation and split into consecutive 500-observation POSTs.
  Two configured targets that share an identity merge into that group.
  Identical duplicate observations dedupe by `external_key`. Divergent
  observations with the same `external_key` fail closed with
  `identity-conflict`; config order never selects a winner.
  Two nodes each run their own invocation and each POST once. Hub
  dedupe is by `external_key`. Idempotency key
  `brigade:{identity}:{sha256(sorted fingerprints)[:24]}`. Empty
  discovery still posts an empty batch so the hub cursor advances. A
  same-fingerprint replay still updates hub cursor `last_success_at`
  and `updated_at`. Each observation fingerprint covers every
  source-authoritative field emitted by the adapter: `external_key`, `title`,
  `acceptance`, `external_state`, `priority`, `dependencies`, `evidence_refs`,
  `source_policy`, `proposed_status`, and `external_updated_at`. This lets text,
  priority, evidence, and terminal-state changes advance the idempotency key,
  and keeps two payloads that differ only in when the source last changed from
  collapsing onto one key.
- The adapter emits `external_updated_at` from the public task's `updated_at`,
  bounded to 64 characters and only when the hub can parse it as an instant.
  That is the source revision the hub's rollback guard compares: without one,
  a Brigade identity has no high-water and an expired-key replay is
  last-write-wins. A revision the guard cannot compare is worse than none, so
  an unparseable value is omitted rather than forwarded.
- Title and acceptance bounds come from `worklore_validate`
  (`TITLE_MAX`, `ACCEPTANCE_MAX_ITEMS`, `ACCEPTANCE_ITEM_MAX`) rather than
  local copies, so the adapter cannot drift from the hub contract.
- Isolation is per task. A task the adapter cannot bound is skipped and
  counted rather than failing its target, so one unsafe task never stops
  the other tasks in the same ledger from reaching the hub.
  `observations_for_target` returns the normalized observations with a
  bounded `skipped` count; `sync_brigade` sums it into the result and the
  retry receipt. A payload that is not a task list, and a list entry that
  is not an object, remain `retryable-adapter-failure`: those are
  malformed command output, not one unsafe task. Skipping never widens the
  posted batch, so the idempotency key still covers exactly the
  observations posted.
- Hub refusals keep a stable code instead of collapsing to
  `hub-unavailable`. `401` and `403` are `hub-unauthorized`, `409` is
  `hub-conflict`, any other `4xx` is `hub-rejected`, and transport
  failures, `5xx`, and unparsable refusals stay `hub-unavailable`. The
  code is raised on `BrigadeSyncError` and recorded as the receipt
  `last_error_code`; the hub message itself is never surfaced. Only
  `hub-unavailable` means retrying the same batch can succeed.
- In-house Brigade commits include a coding-agent `Co-Authored-By`
  trailer. Known-good identities are in `AGENTS.md`.

## Task 1: normalize public task JSON

**Files:**

- Create: `src/brigade/worklore_brigade.py`
- Create: `tests/test_worklore_brigade.py`

- [x] Write the failing tests

```python
from brigade import worklore_brigade as adapter


def test_normalize_pending_task_strips_paths_and_keeps_acceptance():
    observation = adapter.normalize_task(
        {
            "id": "abc123",
            "text": "Add Worklore store",
            "status": "pending",
            "priority": "high",
            "acceptance": ["pytest tests/test_worklore_store.py passes"],
            "metadata": {
                "verify_run_id": "run-xyz",
                "receipt_id": "rec-1",
                "run_path": "/var/lib/brigade/targets/brigade/.brigade/runs/run-xyz",
            },
        },
        identity="escoffier-labs/brigade",
        edges=[{"source": "abc123", "target": "def456", "type": "blocks"}],
    )
    assert observation["external_key"] == "escoffier-labs/brigade#abc123"
    assert observation["link_type"] == "brigade"
    assert observation["acceptance"] == ["pytest tests/test_worklore_store.py passes"]
    assert observation["dependencies"] == [{"type": "blocks", "id": "def456"}]
    assert observation["evidence_refs"] == ["run-xyz", "rec-1"]
    assert observation["source_policy"] == "eligible"
    dumped = json.dumps(observation)
    assert "/var/lib" not in dumped
    assert "tasks_path" not in observation


def test_done_task_proposes_completed_without_transition_fields():
    observation = adapter.normalize_task(
        {
            "id": "done-1",
            "text": "Finished",
            "status": "done",
            "acceptance": ["shipped"],
            "metadata": {},
        },
        identity="escoffier-labs/brigade",
        edges=[],
    )
    assert observation["source_policy"] == "completed"
    assert observation["proposed_status"] == "completed"
    assert observation["external_state"] == "done"
    assert "status" not in observation


def test_normalize_rejects_home_paths_and_receipt_bodies():
    with pytest.raises(adapter.BrigadeObservationError, match="unknown-field"):
        adapter.normalize_task(
            {
                "id": "bad",
                "text": "Nope",
                "status": "pending",
                "acceptance": ["x"],
                "receipt_body": {"stdout": "secret"},
            },
            identity="escoffier-labs/brigade",
            edges=[],
        )
    with pytest.raises(adapter.BrigadeObservationError, match="field-bound"):
        adapter.normalize_task(
            {
                "id": "bad2",
                "text": "See /workspace/notes/secret",
                "status": "pending",
                "acceptance": ["x"],
            },
            identity="escoffier-labs/brigade",
            edges=[],
        )
```

- [x] Run red through Brigade:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_worklore_brigade.py"]' --capture brigade-work
```

Expect FAIL: `ModuleNotFoundError: No module named 'brigade.worklore_brigade'`.

- [x] Implement `normalize_task`. Title is `text` trimmed to 240
  characters. Reject keys
  `{receipt_body,prompt,transcript,token,secret,password,tasks_path}`.
  Reject string values containing `/home/` or `C:\\Users\\`. Keep only
  dependency ids from edges keyed `source` / `target` / `type` where
  `source == task_id`. Do not keep file paths from footprint metadata.

- [x] Run green through Brigade. Expect `passed`.

- [x] Commit:

```bash
git add src/brigade/worklore_brigade.py tests/test_worklore_brigade.py
git commit -m "$(cat <<'EOF'
feat: normalize Brigade task ledger observations for Worklore

EOF
)"
```

## Task 2: explicit targets and public command

**Files:**

- Create: `src/brigade/worklore_brigade_sync.py`
- Modify: `tests/test_worklore_brigade.py`

- [x] Write the failing tests

```python
from brigade import worklore_brigade_sync as sync


def test_load_config_requires_explicit_targets(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "fleet.toml").write_text("[fleet.worklore.brigade]\n")
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    with pytest.raises(sync.BrigadeSyncError, match="targets"):
        sync.load_brigade_config()


def test_read_tasks_uses_public_command_and_drops_tasks_path(tmp_path, monkeypatch):
    target = tmp_path / "repo"
    target.mkdir()
    recorded = {}

    def fake_run(argv, **kwargs):
        recorded["argv"] = argv
        payload = {
            "tasks_path": str(target / ".brigade" / "work" / "tasks.json"),
            "tasks": [{
                "id": "abc123",
                "text": "Add store",
                "status": "pending",
                "acceptance": ["tests pass"],
                "metadata": {},
            }],
            "edges": [{"source": "abc123", "target": "def456", "type": "blocks"}],
        }
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    tasks, edges = sync.read_public_tasks(target)
    assert recorded["argv"] == ["brigade", "work", "tasks", "--target", str(target), "--all", "--json"]
    assert tasks[0]["id"] == "abc123"
    assert edges[0]["source"] == "abc123"
    assert "tasks_path" not in json.dumps(tasks)


def test_two_nodes_same_identity_share_one_external_key(tmp_path):
    task = {
        "id": "abc123",
        "text": "Shared",
        "status": "pending",
        "acceptance": ["ok"],
        "metadata": {},
    }
    first = sync.observations_for_target(task, edges=[], identity="escoffier-labs/brigade")
    second = sync.observations_for_target(task, edges=[], identity="escoffier-labs/brigade")
    assert first[0]["external_key"] == second[0]["external_key"] == "escoffier-labs/brigade#abc123"
```

- [x] Run red through Brigade. Expect FAIL:
  `ModuleNotFoundError: No module named 'brigade.worklore_brigade_sync'`.

- [x] Implement config loading from `fleet.worklore.brigade` using the
  `fleet.sink` pattern in `src/brigade/fleet_export.py:443-456`. Each
  target must be a table with `path` and `identity`. `path` must exist
  and be a directory at sync time. Implement `read_public_tasks` with a
  30 second timeout. A non-zero command exit becomes
  `BrigadeSyncError("retryable-adapter-failure")` with no stderr replay
  if that stderr contains a path.

- [x] Run green through Brigade. Expect `passed`.

- [x] Commit:

```bash
git add src/brigade/worklore_brigade_sync.py tests/test_worklore_brigade.py
git commit -m "$(cat <<'EOF'
feat: read configured Brigade ledgers through the public task command

EOF
)"
```

## Task 3: idempotent post and two-node dedupe against a fake hub

**Files:**

- Modify: `src/brigade/worklore_brigade_sync.py`
- Modify: `tests/test_worklore_brigade.py`

Do not modify `worklore_client.py`.

- [x] Write the failing tests

```python
from tests.support import PRIVATE_FILE_MODE, assert_private_mode


def test_sync_merges_targets_that_share_identity(tmp_path, monkeypatch):
    posts = []
    seen = {}

    def fake_import_batch(payload):
        posts.append(payload)
        created = 0
        updated = 0
        for obs in payload["observations"]:
            key = obs["external_key"]
            if key in seen:
                updated += 1
            else:
                seen[key] = True
                created += 1
        return {"created": created, "updated": updated}

    monkeypatch.setattr(sync.worklore_client, "import_batch", fake_import_batch)
    monkeypatch.setattr(
        sync,
        "read_public_tasks",
        lambda target: ([{
            "id": "abc123",
            "text": "Shared",
            "status": "pending",
            "acceptance": ["ok"],
            "metadata": {},
        }], []),
    )
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    (tmp_path / "node-a").mkdir()
    (tmp_path / "node-b").mkdir()
    result = sync.sync_brigade(
        targets=[
            sync.Target(path=tmp_path / "node-a", identity="escoffier-labs/brigade"),
            sync.Target(path=tmp_path / "node-b", identity="escoffier-labs/brigade"),
        ]
    )
    assert len(posts) == 1
    assert posts[0]["source_type"] == "brigade"
    assert set(posts[0]) >= {"adapter_id", "source_type", "idempotency_key", "observations"}
    assert [obs["external_key"] for obs in posts[0]["observations"]] == ["escoffier-labs/brigade#abc123"]
    assert result["created"] == 1


def test_sync_two_nodes_post_same_external_key(tmp_path, monkeypatch):
    posts = []
    seen = {}

    def fake_import_batch(payload):
        posts.append(payload)
        created = 0
        updated = 0
        for obs in payload["observations"]:
            key = obs["external_key"]
            if key in seen:
                updated += 1
            else:
                seen[key] = True
                created += 1
        return {"created": created, "updated": updated}

    monkeypatch.setattr(sync.worklore_client, "import_batch", fake_import_batch)
    monkeypatch.setattr(
        sync,
        "read_public_tasks",
        lambda target: ([{
            "id": "abc123",
            "text": "Shared",
            "status": "pending",
            "acceptance": ["ok"],
            "metadata": {},
        }], []),
    )
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    (tmp_path / "node-a").mkdir()
    (tmp_path / "node-b").mkdir()
    first = sync.sync_brigade(targets=[sync.Target(path=tmp_path / "node-a", identity="escoffier-labs/brigade")])
    second = sync.sync_brigade(targets=[sync.Target(path=tmp_path / "node-b", identity="escoffier-labs/brigade")])
    keys = [obs["external_key"] for payload in posts for obs in payload["observations"]]
    assert keys == ["escoffier-labs/brigade#abc123", "escoffier-labs/brigade#abc123"]
    assert first["created"] == 1
    assert second["created"] == 0
    receipt = tmp_path / "worklore" / "brigade" / "escoffier-labs-brigade.json"
    assert json.loads(receipt.read_text())["last_error_code"] is None
    assert_private_mode(receipt, PRIVATE_FILE_MODE)


def test_hub_unavailable_is_retryable_and_keeps_local_ledger_usable(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sync.worklore_client,
        "import_batch",
        lambda payload: (_ for _ in ()).throw(sync.worklore_client.FleetClientError("hub-unavailable")),
    )
    monkeypatch.setattr(sync, "read_public_tasks", lambda target: ([], []))
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    with pytest.raises(sync.BrigadeSyncError, match="hub-unavailable"):
        sync.sync_brigade(targets=[sync.Target(path=tmp_path, identity="escoffier-labs/brigade")])
    receipt = tmp_path / "worklore" / "brigade" / "escoffier-labs-brigade.json"
    assert receipt.is_file()
    assert_private_mode(receipt, PRIVATE_FILE_MODE)
```

- [x] Run red through Brigade. Expect FAIL on `sync_brigade` missing.

- [x] Implement `sync_brigade`. Build one logical group per identity. Two
  targets that share an identity merge identical observations, deduped by
  `external_key`; divergent observations for one key fail with
  `identity-conflict`. Split groups into consecutive batches of at most 500.
  Fingerprint every source-authoritative observation field listed in the
  pinned decisions. POST
  `{adapter_id, source_type, idempotency_key, observations}` with
  `source_type="brigade"`. Write the 0600 receipt. Empty discovery still
  posts an empty batch. On hub failure
  raise `BrigadeSyncError("hub-unavailable")` and leave the local
  `.brigade/work/tasks.json` untouched.

- [x] Run green through Brigade. Expect `passed`.

- [x] Commit:

```bash
git add src/brigade/worklore_brigade_sync.py tests/test_worklore_brigade.py
git commit -m "$(cat <<'EOF'
feat: post idempotent Brigade Worklore import batches

EOF
)"
```

## Task 4: CLI

**Files:**

- Modify: `src/brigade/cli/fleet.py`
- Modify: `tests/test_worklore_brigade.py`

- [x] Write the failing test

```python
from brigade.cli import fleet as fleet_cli


def test_sync_brigade_cli_json(monkeypatch, capsys):
    monkeypatch.setattr(
        fleet_cli.worklore_brigade_sync,
        "sync_brigade",
        lambda **kwargs: {"targets": 1, "posted": 1, "created": 1},
    )
    assert fleet_cli._dispatch_work_sync_brigade(argparse.Namespace(json=True)) == 0
    assert '"posted": 1' in capsys.readouterr().out
```

- [x] Run red through Brigade. Expect FAIL: attribute missing.

- [x] Add `brigade fleet work sync-brigade`. Import `worklore_brigade_sync`
  as a module. `_dispatch_work_sync_brigade` reads config, calls
  `worklore_brigade_sync.sync_brigade`, and returns 2 when `targets` is
  missing, 1 on `hub-unavailable` or `retryable-adapter-failure`, 0 on
  success. Default output is counts only. Do not add a separate `_impl`
  function.

- [x] Run green through Brigade:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_worklore_brigade.py"]' --capture brigade-work
```

Expect `passed`.

- [x] Commit:

```bash
git add src/brigade/cli/fleet.py tests/test_worklore_brigade.py
git commit -m "$(cat <<'EOF'
feat: add brigade fleet work sync-brigade

EOF
)"
```

## Verification

All tasks use `./scripts/verify-focused tests/test_worklore_brigade.py`
through `brigade work verify run --capture brigade-work`. Do not point
`targets` at the operator home or this checkout during development. Use
`tmp_path` the way `tests/test_worklore_brigade.py` does.
