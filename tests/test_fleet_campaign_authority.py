"""Exact local lineage inspection, without campaign or launch authority."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from brigade import cli
from brigade.work_cmd import helpers
from brigade.work_cmd.ledger import import_model, tasks_plans


def snapshot(target):
    return {p.relative_to(target): p.read_bytes() if p.is_file() else None for p in target.rglob("*")}


@pytest.fixture
def lineage(tmp_path):
    repo = tmp_path / "private-repo"
    repo.mkdir()
    (tmp_path / ".brigade/repos/actions").mkdir(parents=True)
    (tmp_path / ".brigade/repos.toml").write_text('[[repo]]\nid="repo-a"\npath="private-repo"\n')
    metadata = {"repo_id": "repo-a", "fleet_action_id": "action-a", "source_fingerprint": "a" * 16}
    item = tasks_plans._make_import("synthetic private content", kind="task", source="repo-fleet", metadata=metadata)
    item["id"] = "import-a"
    preview = import_model._task_preview_from_import(item)
    preview["task_type"] = preview.pop("type")
    task = tasks_plans._make_task(**preview)
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
        ("matched", None),
        ("action-missing", "action_missing"),
        ("import-pending", "import_not_promoted"),
        ("task-missing", "task_missing"),
        ("task-mismatch", "task_mismatch"),
    ],
)
def test_generated_lineage_ids_remain_internal(lineage, capsys, case, reason):
    target, repo, actions, refs, action, _, _ = lineage
    title = "Synthetic confidential merger codename"
    sensitive_slug = "synthetic-confidential-merger-codename"
    metadata = {"repo_id": "repo-a", "fleet_action_id": "action-a", "source_fingerprint": "a" * 16}
    item = tasks_plans._make_import(title, kind="task", source="repo-fleet", metadata=metadata)
    preview = import_model._task_preview_from_import(item)
    preview["task_type"] = preview.pop("type")
    task = tasks_plans._make_task(**preview)
    assert sensitive_slug in item["id"]
    assert sensitive_slug in task["id"]
    item.update(status="promoted", task_id=task["id"])
    task["status"] = "done"
    action["dispatch"]["target_import_id"] = item["id"]
    if case == "import-pending":
        item["status"] = "pending"
    elif case == "task-mismatch":
        task["metadata"]["import_id"] = "unrelated-import"
    actions.write_text(json.dumps({"actions": [] if case == "action-missing" else [action]}))
    helpers._imports_path(repo).write_text(json.dumps(item) + "\n")
    helpers._tasks_path(repo).write_text(json.dumps({"version": 1, "tasks": [] if case == "task-missing" else [task]}))
    before = snapshot(target)
    assert invoke(target, refs) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    member = result["members"][0]
    matched = reason is None
    assert member["action_binding"] == ("matched" if matched else "unknown")
    assert member["reason"] == reason
    assert member["local_task_status"] == ("done" if matched else "unknown")
    assert member["specification_verification"] == member["launch_safety"] == "unknown"
    assert result["summary"] == {"inspected": 1, "matched": int(matched), "unknown": int(not matched), "done": False}
    assert title not in output.out + output.err
    assert sensitive_slug not in output.out + output.err
    assert item["id"] not in output.out + output.err
    assert task["id"] not in output.out + output.err
    assert "import_id" not in member
    assert "task_id" not in member
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
        ("task-unrelated", "task_mismatch"),
        ("task-source", "task_mismatch"),
        *[
            (f"task-metadata-{field}", "task_mismatch")
            for field in (
                "import_id",
                "import_kind",
                "import_source",
                "fleet_action_id",
                "repo_id",
                "source_fingerprint",
            )
        ],
        ("task-no-metadata", "task_mismatch"),
        ("action-import-pointer", "lineage_mismatch"),
        ("action-task-pointer", "lineage_mismatch"),
        ("dispatch-task-pointer", "lineage_mismatch"),
        ("action-archived", "action_superseded"),
        ("action-superseded", "action_superseded"),
        ("action-state-missing", "action_status_invalid"),
        ("action-state-junk", "action_status_invalid"),
        ("action-state-object", "action_status_invalid"),
        *[
            (f"{owner}-state-{state}", "lineage_mismatch")
            for owner in ("resolution", "dispatch")
            for state in ("superseded", "dismissed", "stale", "broken-reference", "junk", "object")
        ],
        ("import-pending", "import_not_promoted"),
        ("import-dismissed", "import_dismissed"),
        ("import-state-junk", "import_status_invalid"),
        ("tasks-version", "ledger_refused"),
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
    elif case in {"import-action", "import-repo", "import-fingerprint"}:
        key = {
            "import-action": "fleet_action_id",
            "import-repo": "repo_id",
            "import-fingerprint": "source_fingerprint",
        }[case]
        item["metadata"][key] = "b" * 16
    elif case == "superseded":
        item["status"] = "superseded"
    elif case == "task-unrelated":
        unrelated = tasks_plans._make_task("unrelated private content", source="manual")
        unrelated["id"] = "task-unrelated"
        unrelated["status"] = "done"
        task_rows.append(unrelated)
        item["task_id"] = unrelated["id"]
    elif case == "task-source":
        task["source"] = "repo-fleet"
    elif case.startswith("task-metadata-"):
        task["metadata"][case.removeprefix("task-metadata-")] = "unrelated"
    elif case == "task-no-metadata":
        task.pop("metadata")
    elif case == "action-import-pointer":
        action["target_import_id"] = "import-unrelated"
    elif case == "action-task-pointer":
        action["target_task_id"] = "task-unrelated"
    elif case == "dispatch-task-pointer":
        action["dispatch"]["target_task_id"] = "task-unrelated"
    elif case in {"action-archived", "action-superseded"}:
        action["status"] = case.removeprefix("action-")
    elif case == "action-state-missing":
        action.pop("status")
    elif case.startswith("action-state-"):
        action["status"] = {} if case.endswith("object") else "junk"
    elif case.startswith(("resolution-state-", "dispatch-state-")):
        owner, _, state = case.split("-", 2)
        record = action if owner == "resolution" else action["dispatch"]
        record["resolution_status" if owner == "resolution" else "status"] = {} if state == "object" else state
    elif case in {"import-pending", "import-dismissed", "import-state-junk"}:
        item["status"] = case.removeprefix("import-")
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
    if case == "tasks-version":
        ledger = {"version": True, "tasks": task_rows}
        helpers._tasks_path(repo).write_text(json.dumps(ledger))
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
    task["metadata"]["source_fingerprint"] = fingerprint
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
    target, repo, actions, refs, action, item, task = lineage
    read = os.read
    changed = False

    def changed_read(descriptor, size):
        nonlocal changed
        observed = read(descriptor, size)
        if observed and not changed:
            changed = True
            action["source_fingerprint"] = "b" * 16
            actions.write_text(json.dumps({"actions": [action]}))
        return observed

    monkeypatch.setattr(os, "read", changed_read)
    assert invoke(target, refs) == 0
    member = json.loads(capsys.readouterr().out)["members"][0]
    assert member["reason"] == "lineage_changed"
    assert member["local_task_status"] == member["action_binding"] == "unknown"


@pytest.mark.parametrize("field", ["actions", "imports", "tasks"])
def test_inspect_refuses_oversized_ledgers(lineage, capsys, field):
    from brigade.fleet_campaign_authority import MAX_LEDGER_BYTES

    target, repo, actions, refs, *_ = lineage
    path = {"actions": actions, "imports": helpers._imports_path(repo), "tasks": helpers._tasks_path(repo)}[field]
    path.write_bytes(path.read_bytes() + b" " * MAX_LEDGER_BYTES)
    before = snapshot(target)
    assert invoke(target, refs) == 0
    member = json.loads(capsys.readouterr().out)["members"][0]
    assert member["reason"] == "ledger_refused"
    assert member["local_task_status"] == member["action_binding"] == "unknown"
    assert snapshot(target) == before


@pytest.mark.parametrize("field", ["actions", "imports", "tasks"])
def test_inspect_parses_bounded_bytes_without_compatibility_rereads(lineage, capsys, monkeypatch, field):
    from brigade.repos_cmd import actions_dispatch
    from brigade.work_cmd.ledger import locking

    target, repo, actions, refs, *_ = lineage

    def forbidden(*args, **kwargs):
        pytest.fail("unbounded or compatibility path reader called")

    owner, name = {
        "actions": (actions_dispatch, "_read_actions"),
        "imports": (import_model, "_read_imports"),
        "tasks": (locking, "_read_task_ledger"),
    }[field]
    monkeypatch.setattr(owner, name, forbidden)
    assert invoke(target, refs) == 0
    assert json.loads(capsys.readouterr().out)["members"][0]["action_binding"] == "matched"


@pytest.mark.parametrize("kind", ["file-link", "directory-link", "directory", "fifo", "junction"])
def test_inspect_rejects_links_and_nonregular_ledgers(lineage, capsys, kind):
    target, repo, actions, refs, *_ = lineage
    if kind == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("FIFO fixture requires POSIX mkfifo")
    if kind == "junction" and os.name != "nt":
        pytest.skip("junction fixture requires Windows")
    if kind in {"directory-link", "junction"}:
        original = actions.parent
        moved = original.with_name("original-actions")
        original.rename(moved)
        if kind == "junction":
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(original), str(moved)], capture_output=True)
            assert result.returncode == 0, result.stderr
        else:
            try:
                original.symlink_to(moved, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"directory symlink unavailable: {exc}")
    else:
        raw = actions.read_bytes()
        actions.unlink()
        if kind == "file-link":
            other = actions.with_name("original.json")
            other.write_bytes(raw)
            try:
                actions.symlink_to(other)
            except OSError as exc:
                pytest.skip(f"file symlink unavailable: {exc}")
        elif kind == "directory":
            actions.mkdir()
        else:
            os.mkfifo(actions)
    assert invoke(target, refs) == 0
    member = json.loads(capsys.readouterr().out)["members"][0]
    assert member["reason"] == "ledger_refused"
    assert member["action_binding"] == member["local_task_status"] == "unknown"


@pytest.mark.parametrize("change", ["replace", "grow", "over-cap"])
def test_file_changes_during_descriptor_read_stay_unknown(lineage, capsys, monkeypatch, change):
    from brigade.fleet_campaign_authority import MAX_LEDGER_BYTES

    target, repo, actions, refs, *_ = lineage
    read = os.read
    changed = False
    bytes_read = 0

    def changing_read(descriptor, size):
        nonlocal changed, bytes_read
        raw = read(descriptor, size)
        bytes_read += len(raw)
        if raw and not changed:
            changed = True
            if change == "replace":
                replacement = actions.with_name("replacement.json")
                replacement.write_bytes(actions.read_bytes())
                # Windows refuses overwriting an open data handle. Rename the
                # held file first so this fixture exercises an actual swap.
                actions.rename(actions.with_name("retired.json"))
                replacement.replace(actions)
            else:
                with actions.open("ab") as stream:
                    stream.write(b" " * (MAX_LEDGER_BYTES if change == "over-cap" else 1))
        return raw

    monkeypatch.setattr(os, "read", changing_read)
    assert invoke(target, refs) == 0
    member = json.loads(capsys.readouterr().out)["members"][0]
    assert member["reason"] == "lineage_changed"
    assert member["action_binding"] == member["local_task_status"] == "unknown"
    if change == "over-cap":
        assert bytes_read <= MAX_LEDGER_BYTES + 1


@pytest.mark.parametrize("subset", [False, True])
def test_multiple_members_and_exact_subset_are_read_only(lineage, capsys, subset):
    import copy

    target, repo, actions, refs, action, item, task = lineage
    other = target / "second-private-repo"
    other.mkdir()
    helpers._tasks_path(other).parent.mkdir(parents=True)
    other_item, other_task, other_action = copy.deepcopy((item, task, action))
    other_item.update(id="import-b", task_id="task-b")
    other_item["metadata"].update(repo_id="repo-b", fleet_action_id="action-b")
    other_task.update(id="task-b", status="pending")
    other_task["metadata"].update(repo_id="repo-b", fleet_action_id="action-b", import_id="import-b")
    other_action.update(repo_id="repo-b", fleet_action_id="action-b")
    other_action["dispatch"]["target_import_id"] = "import-b"
    # Recorded optional pointers agree with the production promotion chain.
    action.update(target_import_id="import-a", target_task_id="task-a", resolution_status="completed")
    action["dispatch"].update(target_task_id="task-a", status="dispatched")
    helpers._imports_path(other).parent.mkdir(parents=True)
    helpers._imports_path(other).write_text(json.dumps(other_item) + "\n")
    helpers._tasks_path(other).write_text(json.dumps({"version": 2, "tasks": [other_task], "edges": []}))
    actions.write_text(json.dumps({"actions": [action, other_action]}))
    with (target / ".brigade/repos.toml").open("a") as stream:
        stream.write('[[repo]]\nid="repo-b"\npath="second-private-repo"\n')
    records = json.loads(refs.read_text())
    if not subset:
        records.append({"repo_id": "repo-b", "action_id": "action-b", "source_fingerprint": "a" * 16})
    else:
        helpers._tasks_path(other).write_text("corrupt unselected ledger")
    refs.write_text(json.dumps(records))
    before = snapshot(target)
    assert invoke(target, refs, *(["--repos", "repo-a"] if subset else [])) == 0
    result = json.loads(capsys.readouterr().out)
    count = 1 if subset else 2
    assert result["summary"] == {"inspected": count, "matched": count, "unknown": 0, "done": False}
    assert [row["repo_id"] for row in result["members"]] == (["repo-a"] if subset else ["repo-a", "repo-b"])
    assert all(row["specification_verification"] == row["launch_safety"] == "unknown" for row in result["members"])
    assert snapshot(target) == before
