"""Direct tests for the shared action-queue lifecycle primitives."""

from __future__ import annotations

import json
from pathlib import Path

from brigade import actionqueue, cli

NOW = "2026-06-10T12:00:00+00:00"


def _action(action_id: str, fingerprint: str, status: str = "planned") -> dict:
    return {"action_id": action_id, "source_fingerprint": fingerprint, "status": status}


def test_read_actions_missing_file_returns_empty(tmp_path: Path):
    assert actionqueue.read_actions(tmp_path / "absent.json") == []


def test_read_actions_filters_non_dict_items(tmp_path: Path):
    store = tmp_path / "actions.json"
    store.write_text(json.dumps({"actions": [{"action_id": "a"}, "junk", 3, None]}))
    assert actionqueue.read_actions(store) == [{"action_id": "a"}]


def test_read_actions_non_list_actions_returns_empty(tmp_path: Path):
    store = tmp_path / "actions.json"
    store.write_text(json.dumps({"actions": {"action_id": "a"}}))
    assert actionqueue.read_actions(store) == []


def test_find_action_unique_prefix_match():
    actions = [_action("abc123", "f1"), _action("xyz789", "f2")]
    action, error = actionqueue.find_action(actions, "abc", id_field="action_id", label="action")
    assert error is None
    assert action is actions[0]


def test_find_action_not_found_message():
    action, error = actionqueue.find_action([], "missing", id_field="action_id", label="action")
    assert action is None
    assert error == "action not found: missing"


def test_find_action_ambiguous_prefix_message():
    actions = [_action("abc123", "f1"), _action("abc456", "f2")]
    action, error = actionqueue.find_action(actions, "abc", id_field="action_id", label="action")
    assert action is None
    assert error == "action id is ambiguous: abc"


def test_stamp_status_lifecycle_fields():
    action = _action("a1", "f1")
    actionqueue.stamp_status(action, "active", now=NOW)
    assert action["status"] == "active"
    assert action["started_at"] == NOW
    assert action["updated_at"] == NOW

    actionqueue.stamp_status(action, "done", now=NOW)
    assert action["completed_at"] == NOW

    actionqueue.stamp_status(action, "deferred", now=NOW, reason="later")
    assert action["deferred_at"] == NOW
    assert action["defer_reason"] == "later"


def test_stamp_status_deferred_defaults_reason():
    action = _action("a1", "f1")
    actionqueue.stamp_status(action, "deferred", now=NOW)
    assert action["defer_reason"] == "deferred"


def test_merge_planned_skips_existing_and_archived_fingerprints():
    existing = [_action("e1", "fp-existing")]
    archived = [_action("a1", "fp-archived")]
    planned = [
        _action("p1", "fp-existing"),
        _action("p2", "fp-archived"),
        _action("p3", "fp-new"),
    ]
    created, skipped = actionqueue.merge_planned(existing, archived, planned)
    assert [a["action_id"] for a in created] == ["p3"]
    assert [a["action_id"] for a in skipped] == ["p1", "p2"]
    assert [a["action_id"] for a in existing] == ["e1", "p3"]


def test_merge_planned_dedupes_within_planned_batch():
    existing: list[dict] = []
    planned = [_action("p1", "fp-dup"), _action("p2", "fp-dup")]
    created, skipped = actionqueue.merge_planned(existing, [], planned)
    assert [a["action_id"] for a in created] == ["p1"]
    assert [a["action_id"] for a in skipped] == ["p2"]


def test_split_archived_completed_copies_done_actions():
    actions = [_action("d1", "f1", status="done"), _action("p1", "f2", status="planned")]
    archived, remaining = actionqueue.split_archived_completed(actions, now=NOW)
    assert [a["action_id"] for a in archived] == ["d1"]
    assert archived[0]["status"] == "archived"
    assert archived[0]["archived_at"] == NOW
    assert archived[0]["updated_at"] == NOW
    assert [a["action_id"] for a in remaining] == ["p1"]
    # The archived entry is a copy; the source action is untouched.
    assert actions[0]["status"] == "done"
    assert "archived_at" not in actions[0]


def test_append_archive_noop_on_empty(tmp_path: Path):
    archive = tmp_path / "deep" / "archive.jsonl"
    actionqueue.append_archive(archive, [])
    assert not archive.exists()
    assert not archive.parent.exists()


def test_append_archive_appends_jsonl(tmp_path: Path):
    archive = tmp_path / "deep" / "archive.jsonl"
    actionqueue.append_archive(archive, [_action("a1", "f1")])
    actionqueue.append_archive(archive, [_action("a2", "f2")])
    lines = archive.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["action_id"] == "a1"
    assert json.loads(lines[1])["action_id"] == "a2"


def _seed_center_actions(target: Path, actions: list[dict]) -> None:
    store = target / ".brigade" / "center" / "actions" / "actions.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({"actions": actions}))


def test_cli_center_actions_show_ambiguous_id_exits_nonzero(tmp_path: Path, capsys):
    _seed_center_actions(tmp_path, [_action("abc123", "f1"), _action("abc456", "f2")])
    rc = cli.main(["center", "actions", "show", "abc", "--target", str(tmp_path)])
    assert rc == 2
    assert "action id is ambiguous: abc" in capsys.readouterr().err


def test_cli_center_actions_show_not_found_exits_nonzero(tmp_path: Path, capsys):
    _seed_center_actions(tmp_path, [_action("abc123", "f1")])
    rc = cli.main(["center", "actions", "show", "zzz", "--target", str(tmp_path)])
    assert rc == 1
    assert "action not found: zzz" in capsys.readouterr().err
