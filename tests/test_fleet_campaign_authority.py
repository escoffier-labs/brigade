"""Exact local lineage inspection, without campaign or launch authority."""

from __future__ import annotations

import json

import pytest

from brigade import cli
from brigade.work_cmd import helpers
from brigade.work_cmd.ledger import tasks_plans


def snapshot(target):
    return {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()}


@pytest.fixture
def lineage(tmp_path):
    repo = tmp_path / "private-repo"
    repo.mkdir()
    (tmp_path / ".brigade/repos/actions").mkdir(parents=True)
    (tmp_path / ".brigade/repos.toml").write_text('[[repo]]\nid="repo-a"\npath="private-repo"\n')
    metadata = {"repo_id": "repo-a", "fleet_action_id": "action-a", "source_fingerprint": "a" * 16}
    item = tasks_plans._make_import("synthetic private content", kind="task", source="repo-fleet", metadata=metadata)
    task = tasks_plans._make_task("synthetic private content", source="repo-fleet", metadata=metadata)
    item["id"] = "import-a"
    task["id"] = "task-a"
    item.update(status="promoted", task_id=task["id"])
    task["status"] = "done"
    helpers._imports_path(repo).parent.mkdir(parents=True)
    helpers._imports_path(repo).write_text(json.dumps(item) + "\n")
    helpers._tasks_path(repo).write_text(json.dumps({"version": 1, "tasks": [task]}))
    action = {
        "fleet_action_id": "action-a",
        "repo_id": "repo-a",
        "source_fingerprint": "a" * 16,
        "reviewed_at": "2026-01-01T00:00:00+00:00",
        "status": "active",
        "safe_summary": "synthetic private content",
        "dispatch": {"source_fingerprint": "a" * 16, "target_import_id": item["id"]},
    }
    actions = tmp_path / ".brigade/repos/actions/actions.json"
    actions.write_text(json.dumps({"actions": [action]}))
    refs = tmp_path / "references.json"
    refs.write_text(json.dumps([{"repo_id": "repo-a", "action_id": "action-a", "source_fingerprint": "a" * 16}]))
    return tmp_path, repo, actions, refs, action, item, task


def invoke(target, refs, *extra):
    return cli.main(
        [
            "fleet",
            "campaign",
            "inspect",
            "--target",
            str(target),
            "--campaign-id",
            "campaign-a",
            "--references",
            str(refs),
            "--json",
            *extra,
        ]
    )


def test_inspect_exact_local_lineage_is_read_only(lineage, capsys, monkeypatch):
    from brigade import fleet_client, grokbot_jobs
    from brigade.repos_cmd import actions_dispatch
    from brigade.work_cmd import ledger

    target, repo, actions, refs, action, item, task = lineage

    def forbidden(*args, **kwargs):
        pytest.fail("inspection crossed a write or live authority boundary")

    monkeypatch.setattr(fleet_client, "load_fleet_config", forbidden)
    monkeypatch.setattr(fleet_client, "repo_claim", forbidden)
    monkeypatch.setattr(grokbot_jobs, "enqueue", forbidden)
    monkeypatch.setattr(actions_dispatch, "actions_dispatch_apply", forbidden)
    monkeypatch.setattr(actions_dispatch, "_write_actions", forbidden)
    monkeypatch.setattr(ledger, "_write_imports", forbidden)
    monkeypatch.setattr(ledger, "_write_task_ledger", forbidden)
    before = snapshot(target)
    assert invoke(target, refs) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["kind"] == "fleet-campaign-inspect"
    assert result["campaign_id"] == "campaign-a"
    assert result["read_only"] is True
    assert result["summary"] == {"inspected": 1, "matched": 1, "unknown": 0, "done": False}
    assert result["members"] == [
        {
            "repo_id": "repo-a",
            "action_id": "action-a",
            "source_fingerprint": "a" * 16,
            "import_id": item["id"],
            "task_id": task["id"],
            "action_binding": "matched",
            "reason": None,
            "local_task_status": "done",
            "specification_verification": "unknown",
            "launch_safety": "unknown",
        }
    ]
    assert "synthetic private content" not in output.out + output.err
    assert "private-repo" not in output.out + output.err
    assert str(target) not in output.out + output.err
    assert snapshot(target) == before


@pytest.mark.parametrize(
    "case,reason",
    [
        ("action-prefix", "action_missing"),
        ("action-missing", "action_missing"),
        ("repo-mismatch", "action_mismatch"),
        ("unreviewed", "action_unreviewed"),
        ("stale", "action_mismatch"),
        ("no-fingerprint", "action_mismatch"),
        ("dispatch-stale", "dispatch_mismatch"),
        ("import-prefix", "import_missing"),
        ("import-missing-newer", "import_missing"),
        ("import-action", "import_mismatch"),
        ("import-fingerprint", "import_mismatch"),
        ("import-repo", "import_mismatch"),
        ("superseded", "import_superseded"),
        ("task-prefix", "task_missing"),
        ("task-missing", "task_missing"),
        ("task-status", "task_status_invalid"),
        ("duplicate-action", "action_duplicate"),
        ("duplicate-import", "import_duplicate"),
        ("duplicate-task", "task_duplicate"),
        ("corrupt-actions", "ledger_refused"),
        ("corrupt-imports", "ledger_refused"),
        ("corrupt-tasks", "ledger_refused"),
        ("unsafe-import", "opaque_reference_required"),
        ("unsafe-task", "opaque_reference_required"),
    ],
)
def test_lineage_uncertainty_never_falls_back(lineage, capsys, case, reason):
    target, repo, actions, refs, action, item, task = lineage
    action_rows, import_rows, task_rows = [action], [item], [task]
    if case == "action-prefix":
        refs.write_text(json.dumps([{"repo_id": "repo-a", "action_id": "action", "source_fingerprint": "a" * 16}]))
    elif case == "action-missing":
        action_rows = []
    elif case == "repo-mismatch":
        action["repo_id"] = "repo-b"
    elif case == "unreviewed":
        action.pop("reviewed_at")
    elif case in {"stale", "no-fingerprint"}:
        action["source_fingerprint"] = "b" * 16 if case == "stale" else None
    elif case == "dispatch-stale":
        action["dispatch"]["source_fingerprint"] = "b" * 16
    elif case == "import-prefix":
        action["dispatch"]["target_import_id"] = item["id"][:-1]
    elif case == "import-missing-newer":
        action["dispatch"]["target_import_id"] = "recorded-missing-import"
    elif case.startswith("import-"):
        key = {
            "import-action": "fleet_action_id",
            "import-repo": "repo_id",
            "import-fingerprint": "source_fingerprint",
        }[case]
        item["metadata"][key] = "b" * 16
    elif case == "superseded":
        item["status"] = "superseded"
    elif case == "task-prefix":
        item["task_id"] = task["id"][:-1]
    elif case == "task-missing":
        task_rows = []
    elif case == "task-status":
        task["status"] = "private-invalid-state"
    elif case == "duplicate-action":
        action_rows *= 2
    elif case == "duplicate-import":
        import_rows *= 2
    elif case == "duplicate-task":
        task_rows *= 2
    elif case == "unsafe-import":
        action["dispatch"]["target_import_id"] = "/private/unsafe"
    elif case == "unsafe-task":
        item["task_id"] = "/private/unsafe"
    actions.write_text(json.dumps({"actions": action_rows}))
    helpers._imports_path(repo).write_text("".join(json.dumps(row) + "\n" for row in import_rows))
    helpers._tasks_path(repo).write_text(json.dumps({"version": 1, "tasks": task_rows}))
    if case == "corrupt-actions":
        actions.write_text('{"actions": [false]}')
    elif case == "corrupt-imports":
        with helpers._imports_path(repo).open("a") as stream:
            stream.write("{private malformed record\n")
    elif case == "corrupt-tasks":
        helpers._tasks_path(repo).write_text('{"tasks":"private malformed ledger"}')
    before = snapshot(target)
    assert invoke(target, refs) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    member = result["members"][0]
    assert member["action_binding"] == "unknown"
    assert member["reason"] == reason
    assert member["local_task_status"] == "unknown"
    assert member["specification_verification"] == member["launch_safety"] == "unknown"
    assert result["summary"] == {"inspected": 1, "matched": 0, "unknown": 1, "done": False}
    assert "private" not in output.out + output.err
    assert snapshot(target) == before


@pytest.mark.parametrize(
    "case",
    ["duplicate", "empty", "extra", "invalid-key", "invalid-fingerprint", "too-many", "too-large", "missing-repo"],
)
def test_input_refusals_are_bounded_and_opaque(lineage, capsys, case):
    target, repo, actions, refs, action, item, task = lineage
    records = [{"repo_id": "repo-a", "action_id": "action-a", "source_fingerprint": "a" * 16}]
    if case == "duplicate":
        records *= 2
    elif case == "empty":
        records = []
    elif case == "extra":
        records[0]["prompt"] = "private input"
    elif case == "invalid-key":
        records[0]["action_id"] = "/private/path"
    elif case == "invalid-fingerprint":
        records[0]["source_fingerprint"] = "A" * 64
    elif case == "too-many":
        records *= 257
    elif case == "missing-repo":
        records[0]["repo_id"] = "repo-b"
    refs.write_text(json.dumps(records))
    if case == "too-large":
        refs.write_bytes(b" " * (256 * 1024 + 1))
    before = snapshot(target)
    assert invoke(target, refs) == 2
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["kind"] == "fleet-campaign-inspect"
    assert result["read_only"] is True
    assert "error" in result
    assert "private" not in output.out + output.err
    assert snapshot(target) == before


def test_missing_workspace_is_never_initialized(tmp_path, capsys):
    refs = tmp_path / "references.json"
    refs.write_text("[]")
    target = tmp_path / "absent"
    assert invoke(target, refs) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "repository_selection_refused"
    assert not target.exists()


@pytest.mark.parametrize("status", ["pending", "in_progress", "blocked", "deferred", "done", "cancelled", "dismissed"])
def test_storage_fingerprint_and_local_status_remain_observations(lineage, capsys, status):
    target, repo, actions, refs, action, item, task = lineage
    fingerprint = "a" * 64
    action["source_fingerprint"] = action["dispatch"]["source_fingerprint"] = fingerprint
    item["metadata"]["source_fingerprint"] = fingerprint
    task["status"] = status
    actions.write_text(json.dumps({"actions": [action]}))
    helpers._imports_path(repo).write_text(json.dumps(item) + "\n")
    helpers._tasks_path(repo).write_text(json.dumps({"version": 1, "tasks": [task]}))
    refs.write_text(json.dumps([{"repo_id": "repo-a", "action_id": "action-a", "source_fingerprint": fingerprint}]))
    assert invoke(target, refs) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["members"][0]["local_task_status"] == status
    assert result["members"][0]["action_binding"] == "matched"
    assert result["members"][0]["specification_verification"] == result["members"][0]["launch_safety"] == "unknown"
    assert result["summary"]["done"] is False


def test_changed_action_generation_stays_unknown(lineage, capsys, monkeypatch):
    from brigade.repos_cmd import actions_dispatch

    target, repo, actions, refs, action, item, task = lineage
    read_actions = actions_dispatch._read_actions

    def changed_read(target):
        observed = read_actions(target)
        action["source_fingerprint"] = "b" * 16
        actions.write_text(json.dumps({"actions": [action]}))
        return observed

    monkeypatch.setattr(actions_dispatch, "_read_actions", changed_read)
    assert invoke(target, refs) == 0
    member = json.loads(capsys.readouterr().out)["members"][0]
    assert member["reason"] == "lineage_changed"
    assert member["local_task_status"] == member["action_binding"] == "unknown"
