"""Cross-repo campaign ready aggregation over per-repo work graphs (#814)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from brigade import work_cmd
from brigade.work_cmd import campaign as campaign_mod
from brigade.work_cmd import partition as partition_mod

from tests.work_cmd_test_helpers import _init_git_repo


def _add(target: Path, text: str, **kwargs):
    task, created = work_cmd._add_task(target, text, **kwargs)
    assert created
    return task


def _write_campaign(workspace: Path, name: str, payload: dict) -> Path:
    path = workspace / ".brigade" / "campaigns" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def _member_repo(workspace: Path, name: str) -> Path:
    repo = workspace / name
    repo.mkdir()
    _init_git_repo(repo)
    return repo


def test_campaign_aggregates_ready_with_qualified_ids(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lib = _member_repo(workspace, "lib")
    app = _member_repo(workspace, "app")

    clock = {"n": 0}

    def _now():
        clock["n"] += 1
        return datetime(2026, 8, 9, 18, 0, clock["n"], tzinfo=timezone.utc)

    monkeypatch.setattr(work_cmd.helpers, "_now", _now)

    lib_task = _add(lib, "Land library API")
    app_task = _add(app, "Wire consumer")

    _write_campaign(
        workspace,
        "burn-down",
        {
            "version": 1,
            "name": "burn-down",
            "members": [
                {"id": "lib", "path": "lib"},
                {"id": "app", "path": "app"},
            ],
            "edges": [],
        },
    )

    assert work_cmd.ready(target=workspace, campaign="burn-down", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["campaign"] == "burn-down"
    assert payload["member_count"] == 2
    assert payload["ready_count"] == 2
    ready_ids = {item["id"] for item in payload["ready"]}
    assert ready_ids == {
        campaign_mod.qualify_task_id("lib", lib_task["id"]),
        campaign_mod.qualify_task_id("app", app_task["id"]),
    }
    by_id = {item["id"]: item for item in payload["ready"]}
    lib_qualified = campaign_mod.qualify_task_id("lib", lib_task["id"])
    assert by_id[lib_qualified]["repo"] == "lib"
    assert by_id[lib_qualified]["local_id"] == lib_task["id"]


def test_cross_repo_blocks_gate_campaign_readiness(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lib = _member_repo(workspace, "lib")
    app = _member_repo(workspace, "app")

    clock = {"n": 0}

    def _now():
        clock["n"] += 1
        return datetime(2026, 8, 9, 18, 10, clock["n"], tzinfo=timezone.utc)

    monkeypatch.setattr(work_cmd.helpers, "_now", _now)

    lib_task = _add(lib, "Ship shared package")
    app_task = _add(app, "Consume shared package")
    lib_qid = campaign_mod.qualify_task_id("lib", lib_task["id"])
    app_qid = campaign_mod.qualify_task_id("app", app_task["id"])

    _write_campaign(
        workspace,
        "ordered",
        {
            "version": 1,
            "name": "ordered",
            "members": [
                {"id": "lib", "path": "lib"},
                {"id": "app", "path": "app"},
            ],
            "edges": [{"type": "blocks", "source": lib_qid, "target": app_qid}],
        },
    )

    assert work_cmd.ready(target=workspace, campaign="ordered", explain=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    ready_ids = {item["id"] for item in payload["ready"]}
    assert lib_qid in ready_ids
    assert app_qid not in ready_ids
    blocked = {item["id"]: item for item in payload["blocked"]}
    assert app_qid in blocked
    assert blocked[app_qid]["reason"] == "open_blockers"
    assert any(blocker["id"] == lib_qid for blocker in blocked[app_qid]["blockers"])

    # Completing the blocker in its own ledger unblocks the consumer at aggregation time.
    assert work_cmd.task_done(target=lib, task_id=lib_task["id"]) == 0
    capsys.readouterr()
    assert work_cmd.ready(target=workspace, campaign="ordered", json_output=True) == 0
    after = json.loads(capsys.readouterr().out)
    assert {item["id"] for item in after["ready"]} == {app_qid}


def test_removing_campaign_leaves_member_ledgers_unchanged(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lib = _member_repo(workspace, "lib")
    app = _member_repo(workspace, "app")

    clock = {"n": 0}

    def _now():
        clock["n"] += 1
        return datetime(2026, 8, 9, 18, 20, clock["n"], tzinfo=timezone.utc)

    monkeypatch.setattr(work_cmd.helpers, "_now", _now)

    lib_task = _add(lib, "Lib work")
    app_task = _add(app, "App work")
    lib_before = (lib / ".brigade" / "work" / "tasks.json").read_text()
    app_before = (app / ".brigade" / "work" / "tasks.json").read_text()

    campaign_path = _write_campaign(
        workspace,
        "ephemeral",
        {
            "version": 1,
            "name": "ephemeral",
            "members": [
                {"id": "lib", "path": "lib"},
                {"id": "app", "path": "app"},
            ],
            "edges": [
                {
                    "type": "blocks",
                    "source": campaign_mod.qualify_task_id("lib", lib_task["id"]),
                    "target": campaign_mod.qualify_task_id("app", app_task["id"]),
                }
            ],
        },
    )

    assert work_cmd.ready(target=workspace, campaign="ephemeral", json_output=True) == 0
    capsys.readouterr()
    assert (lib / ".brigade" / "work" / "tasks.json").read_text() == lib_before
    assert (app / ".brigade" / "work" / "tasks.json").read_text() == app_before

    campaign_path.unlink()
    assert work_cmd.ready(target=workspace, campaign="ephemeral", json_output=True) == 1
    err = capsys.readouterr().err
    assert "campaign not found" in err
    assert (lib / ".brigade" / "work" / "tasks.json").read_text() == lib_before
    assert (app / ".brigade" / "work" / "tasks.json").read_text() == app_before

    # Per-repo ready still works without any campaign file.
    assert work_cmd.ready(target=lib, json_output=True) == 0
    lib_ready = json.loads(capsys.readouterr().out)
    assert {item["id"] for item in lib_ready["ready"]} == {lib_task["id"]}


def test_campaign_parallel_safe_uses_wave_aggregation_seam(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lib = _member_repo(workspace, "lib")

    clock = {"n": 0}

    def _now():
        clock["n"] += 1
        return datetime(2026, 8, 9, 18, 30, clock["n"], tzinfo=timezone.utc)

    monkeypatch.setattr(work_cmd.helpers, "_now", _now)
    _add(lib, "Solo task")
    _write_campaign(
        workspace,
        "solo",
        {
            "version": 1,
            "name": "solo",
            "members": [{"id": "lib", "path": "lib"}],
        },
    )

    assert work_cmd.ready(target=workspace, campaign="solo", parallel_safe=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["parallel_safe"] is True
    assert payload["partition_mode"] == partition_mod.MODE_CAMPAIGN_DEFERRED
    assert payload["partition_degraded"] is True
    assert payload["wave_count"] == 0
    assert payload["waves"] == []
    assert "cross-repo wave aggregation" in payload["partition_degraded_reason"]


def test_campaign_config_validation_rejects_bad_edges(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_campaign(
        workspace,
        "bad",
        {
            "version": 1,
            "name": "bad",
            "members": [{"id": "lib", "path": "lib"}],
            "edges": [{"type": "blocks", "source": "lib:abc", "target": "missing:def"}],
        },
    )
    try:
        campaign_mod.load_campaign(workspace, "bad")
        raise AssertionError("expected CampaignError")
    except campaign_mod.CampaignError as exc:
        assert exc.reason == campaign_mod.REASON_INVALID_CAMPAIGN
        assert "unknown member" in str(exc)


def test_campaign_rejects_unsupported_edge_types(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_campaign(
        workspace,
        "typed",
        {
            "version": 1,
            "name": "typed",
            "members": [
                {"id": "lib", "path": "lib"},
                {"id": "app", "path": "app"},
            ],
            "edges": [
                {
                    "type": "parent-child",
                    "source": "lib:parent",
                    "target": "app:child",
                }
            ],
        },
    )
    try:
        campaign_mod.load_campaign(workspace, "typed")
        raise AssertionError("expected CampaignError")
    except campaign_mod.CampaignError as exc:
        assert exc.reason == campaign_mod.REASON_INVALID_CAMPAIGN
        assert "blocks-only" in str(exc)


def test_campaign_path_argument_and_missing_member_fails_closed(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lib = _member_repo(workspace, "lib")

    clock = {"n": 0}

    def _now():
        clock["n"] += 1
        return datetime(2026, 8, 9, 18, 40, clock["n"], tzinfo=timezone.utc)

    monkeypatch.setattr(work_cmd.helpers, "_now", _now)
    _add(lib, "Still ready")
    path = _write_campaign(
        workspace,
        "mixed",
        {
            "version": 1,
            "name": "mixed",
            "members": [
                {"id": "lib", "path": "lib"},
                {"id": "gone", "path": "does-not-exist"},
            ],
        },
    )

    # Missing configured members must not yield exit-0 authoritative readiness.
    assert work_cmd.ready(target=workspace, campaign=str(path), json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == campaign_mod.REASON_MISSING_MEMBER
    assert payload["member_errors"][0]["member_id"] == "gone"
    assert "ready" not in payload


def test_campaign_dangling_edge_endpoint_fails_closed(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lib = _member_repo(workspace, "lib")
    app = _member_repo(workspace, "app")

    clock = {"n": 0}

    def _now():
        clock["n"] += 1
        return datetime(2026, 8, 9, 18, 50, clock["n"], tzinfo=timezone.utc)

    monkeypatch.setattr(work_cmd.helpers, "_now", _now)
    lib_task = _add(lib, "Real library task")
    app_task = _add(app, "Consumer that would look ready under orphan silence")
    _write_campaign(
        workspace,
        "dangling",
        {
            "version": 1,
            "name": "dangling",
            "members": [
                {"id": "lib", "path": "lib"},
                {"id": "app", "path": "app"},
            ],
            "edges": [
                {
                    "type": "blocks",
                    "source": campaign_mod.qualify_task_id("lib", "does-not-exist"),
                    "target": campaign_mod.qualify_task_id("app", app_task["id"]),
                }
            ],
        },
    )

    assert work_cmd.ready(target=workspace, campaign="dangling", json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == campaign_mod.REASON_DANGLING_EDGE
    assert payload["dangling_endpoints"]
    # Local orphan-blocker silence must not apply to campaign edges.
    assert work_cmd.ready(target=app, json_output=True) == 0
    local = json.loads(capsys.readouterr().out)
    assert app_task["id"] in {item["id"] for item in local["ready"]}
    assert lib_task["id"]  # keep member ledger populated for the assertion above


def test_campaign_ready_preserves_seat_class_spend_by(tmp_path, monkeypatch, capsys):
    """Merged #815 annotations must survive campaign aggregation (#814 lens)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lib = _member_repo(workspace, "lib")

    clock = {"n": 0}

    def _now():
        clock["n"] += 1
        return datetime(2026, 8, 9, 19, 0, clock["n"], tzinfo=timezone.utc)

    monkeypatch.setattr(work_cmd.helpers, "_now", _now)
    task = _add(
        lib,
        "Annotated library work",
        seat_class="judgment",
        spend_by="2026-08-15T12:00:00Z",
    )
    _write_campaign(
        workspace,
        "annotated",
        {
            "version": 1,
            "name": "annotated",
            "members": [{"id": "lib", "path": "lib"}],
        },
    )

    assert work_cmd.ready(target=workspace, campaign="annotated", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    qualified = campaign_mod.qualify_task_id("lib", task["id"])
    item = next(entry for entry in payload["ready"] if entry["id"] == qualified)
    assert item["seat_class"] == "judgment"
    assert item["spend_by"] == "2026-08-15T12:00:00Z"
