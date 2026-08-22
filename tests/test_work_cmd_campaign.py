"""Cross-repo campaign ready aggregation over per-repo work graphs (#814)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from brigade import work_cmd
from brigade.work_cmd import campaign as campaign_mod
from brigade.work_cmd import footprint as footprint_mod
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


def _set_pending_footprint(target: Path, task_id: str, *, files: list[str], symbol_ids: list[str] | None = None):
    """Stamp a refined footprint while keeping the task pending (ready-eligible)."""
    ledger = work_cmd._read_task_ledger(target)
    task = next(item for item in ledger["tasks"] if item["id"] == task_id)
    footprint_mod.set_task_footprint(
        task,
        footprint_mod.normalize_footprint(
            {
                "files": files,
                "symbol_ids": symbol_ids or [],
                "snapshot_hash": "",
            },
            phase=footprint_mod.PHASE_REFINED,
        ),
    )
    work_cmd._write_task_ledger(target, ledger)


def _clock(monkeypatch, hour: int = 18, minute: int = 30):
    clock = {"n": 0}

    def _now():
        clock["n"] += 1
        return datetime(2026, 8, 9, hour, minute, clock["n"], tzinfo=timezone.utc)

    monkeypatch.setattr(work_cmd.helpers, "_now", _now)
    return clock


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
    missing = json.loads(capsys.readouterr().out)
    assert missing["reason"] == campaign_mod.REASON_MISSING_CAMPAIGN
    assert "campaign not found" in missing["error"]
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
    _clock(monkeypatch, minute=30)
    task = _add(lib, "Solo task")
    _set_pending_footprint(lib, task["id"], files=["src/solo.py"])
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
    qualified = campaign_mod.qualify_task_id("lib", task["id"])
    assert payload["parallel_safe"] is True
    assert payload["partition_mode"] == partition_mod.MODE_FILE_OVERLAP
    assert payload["wave_count"] == 1
    assert payload["waves"][0]["task_ids"] == [qualified]
    assert payload["waves"][0]["exclusive"] is False
    assert "campaign_deferred" not in json.dumps(payload)
    assert "cross-repo wave aggregation" not in str(payload.get("partition_degraded_reason") or "")


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


def test_campaign_unreadable_member_ledger_fails_closed(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lib = _member_repo(workspace, "lib")
    broken = _member_repo(workspace, "broken")

    clock = {"n": 0}

    def _now():
        clock["n"] += 1
        return datetime(2026, 8, 9, 18, 55, clock["n"], tzinfo=timezone.utc)

    monkeypatch.setattr(work_cmd.helpers, "_now", _now)
    _add(lib, "Healthy member task")
    tasks_path = broken / ".brigade" / "work" / "tasks.json"
    tasks_path.parent.mkdir(parents=True, exist_ok=True)
    tasks_path.write_text("{not-json\n")
    _write_campaign(
        workspace,
        "unreadable",
        {
            "version": 1,
            "name": "unreadable",
            "members": [
                {"id": "lib", "path": "lib"},
                {"id": "broken", "path": "broken"},
            ],
        },
    )

    assert work_cmd.ready(target=workspace, campaign="unreadable", json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == campaign_mod.REASON_UNREADABLE_MEMBER
    assert any(err.get("member_id") == "broken" for err in payload["member_errors"])
    assert "ready" not in payload


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
    assert (
        work_cmd.task_add(
            target=lib,
            text="Annotated library work",
            seat_class="judgment",
            spend_by="2026-08-15T12:00:00Z",
        )
        == 0
    )
    out = capsys.readouterr().out
    local_id = out.split("task: ", 1)[1].splitlines()[0]
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
    qualified = campaign_mod.qualify_task_id("lib", local_id)
    item = next(entry for entry in payload["ready"] if entry["id"] == qualified)
    assert item["seat_class"] == "judgment"
    assert item["spend_by"] == "2026-08-15T12:00:00Z"


def test_campaign_parallel_safe_returns_deterministic_nonempty_waves(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lib = _member_repo(workspace, "lib")
    app = _member_repo(workspace, "app")
    _clock(monkeypatch, minute=5)
    lib_task = _add(lib, "Lib disjoint")
    app_task = _add(app, "App disjoint")
    _set_pending_footprint(lib, lib_task["id"], files=["src/lib.py"])
    _set_pending_footprint(app, app_task["id"], files=["src/app.py"])
    _write_campaign(
        workspace,
        "together",
        {
            "version": 1,
            "name": "together",
            "members": [
                {"id": "lib", "path": "lib"},
                {"id": "app", "path": "app"},
            ],
        },
    )
    lib_qid = campaign_mod.qualify_task_id("lib", lib_task["id"])
    app_qid = campaign_mod.qualify_task_id("app", app_task["id"])

    assert work_cmd.ready(target=workspace, campaign="together", parallel_safe=True, json_output=True) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["wave_count"] == 1
    assert first["waves"][0]["task_ids"] == [lib_qid, app_qid]
    assert first["waves"][0]["exclusive"] is False

    assert work_cmd.ready(target=workspace, campaign="together", parallel_safe=True, json_output=True) == 0
    second = json.loads(capsys.readouterr().out)
    assert [wave["task_ids"] for wave in second["waves"]] == [wave["task_ids"] for wave in first["waves"]]


def test_campaign_parallel_safe_serializes_overlapping_member_footprints(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lib = _member_repo(workspace, "lib")
    app = _member_repo(workspace, "app")
    _clock(monkeypatch, minute=10)
    lib_a = _add(lib, "Lib shared A")
    lib_b = _add(lib, "Lib shared B")
    app_c = _add(app, "App helpers")
    _set_pending_footprint(lib, lib_a["id"], files=["src/shared.py", "src/a.py"])
    _set_pending_footprint(lib, lib_b["id"], files=["src/shared.py", "src/b.py"])
    _set_pending_footprint(app, app_c["id"], files=["src/helpers.py"])
    _write_campaign(
        workspace,
        "overlap",
        {
            "version": 1,
            "name": "overlap",
            "members": [
                {"id": "lib", "path": "lib"},
                {"id": "app", "path": "app"},
            ],
        },
    )
    lib_a_qid = campaign_mod.qualify_task_id("lib", lib_a["id"])
    lib_b_qid = campaign_mod.qualify_task_id("lib", lib_b["id"])
    app_qid = campaign_mod.qualify_task_id("app", app_c["id"])

    assert work_cmd.ready(target=workspace, campaign="overlap", parallel_safe=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["wave_count"] == 2
    assert payload["waves"][0]["task_ids"] == [lib_a_qid, app_qid]
    assert payload["waves"][1]["task_ids"] == [lib_b_qid]


def test_campaign_parallel_safe_shares_wave_for_matching_relative_paths(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lib = _member_repo(workspace, "lib")
    app = _member_repo(workspace, "app")
    _clock(monkeypatch, minute=15)
    lib_task = _add(lib, "Lib shared name")
    app_task = _add(app, "App shared name")
    _set_pending_footprint(lib, lib_task["id"], files=["src/shared.py"])
    _set_pending_footprint(app, app_task["id"], files=["src/shared.py"])
    _write_campaign(
        workspace,
        "names",
        {
            "version": 1,
            "name": "names",
            "members": [
                {"id": "lib", "path": "lib"},
                {"id": "app", "path": "app"},
            ],
        },
    )

    assert work_cmd.ready(target=workspace, campaign="names", parallel_safe=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["wave_count"] == 1
    assert payload["waves"][0]["task_ids"] == [
        campaign_mod.qualify_task_id("lib", lib_task["id"]),
        campaign_mod.qualify_task_id("app", app_task["id"]),
    ]


def test_campaign_parallel_safe_empty_footprint_exclusive_only_inside_member(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lib = _member_repo(workspace, "lib")
    app = _member_repo(workspace, "app")
    _clock(monkeypatch, minute=20)
    empty = _add(lib, "Unknown lib footprint")
    known = _add(lib, "Known lib files")
    app_task = _add(app, "App known files")
    _set_pending_footprint(lib, known["id"], files=["src/known.py"])
    _set_pending_footprint(app, app_task["id"], files=["src/known.py"])
    assert empty["metadata"]["footprint"]["files"] == []
    _write_campaign(
        workspace,
        "empty",
        {
            "version": 1,
            "name": "empty",
            "members": [
                {"id": "lib", "path": "lib"},
                {"id": "app", "path": "app"},
            ],
        },
    )
    empty_qid = campaign_mod.qualify_task_id("lib", empty["id"])
    known_qid = campaign_mod.qualify_task_id("lib", known["id"])
    app_qid = campaign_mod.qualify_task_id("app", app_task["id"])

    assert work_cmd.ready(target=workspace, campaign="empty", parallel_safe=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["wave_count"] == 2
    assert payload["waves"][0]["task_ids"] == [empty_qid, app_qid]
    assert payload["waves"][0]["exclusive"] is False
    assert payload["waves"][1]["task_ids"] == [known_qid]
    lib_ids_by_wave = [
        [task_id for task_id in wave["task_ids"] if task_id.startswith("lib:")] for wave in payload["waves"]
    ]
    assert [empty_qid] in lib_ids_by_wave
    assert [known_qid] in lib_ids_by_wave


def test_campaign_parallel_safe_blocks_gate_readiness_before_composition(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lib = _member_repo(workspace, "lib")
    app = _member_repo(workspace, "app")
    _clock(monkeypatch, minute=25)
    lib_task = _add(lib, "Ship shared package")
    app_task = _add(app, "Consume shared package")
    _set_pending_footprint(lib, lib_task["id"], files=["src/shared.py"])
    _set_pending_footprint(app, app_task["id"], files=["src/shared.py"])
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

    assert (
        work_cmd.ready(
            target=workspace,
            campaign="ordered",
            explain=True,
            parallel_safe=True,
            json_output=True,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    ready_ids = {item["id"] for item in payload["ready"]}
    assert ready_ids == {lib_qid}
    assert app_qid not in ready_ids
    assert payload["wave_count"] == 1
    assert payload["waves"][0]["task_ids"] == [lib_qid]
    blocked = {item["id"]: item for item in payload["blocked"]}
    assert app_qid in blocked
    assert blocked[app_qid]["reason"] == "open_blockers"


def test_campaign_parallel_safe_missing_or_unreadable_member_fails_closed(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lib = _member_repo(workspace, "lib")
    broken = _member_repo(workspace, "broken")
    _clock(monkeypatch, minute=40)
    _add(lib, "Healthy member task")
    _write_campaign(
        workspace,
        "missing",
        {
            "version": 1,
            "name": "missing",
            "members": [
                {"id": "lib", "path": "lib"},
                {"id": "gone", "path": "does-not-exist"},
            ],
        },
    )
    assert work_cmd.ready(target=workspace, campaign="missing", parallel_safe=True, json_output=True) == 1
    missing = json.loads(capsys.readouterr().out)
    assert missing["reason"] == campaign_mod.REASON_MISSING_MEMBER
    assert "ready" not in missing
    assert "waves" not in missing

    tasks_path = broken / ".brigade" / "work" / "tasks.json"
    tasks_path.parent.mkdir(parents=True, exist_ok=True)
    tasks_path.write_text("{not-json\n")
    _write_campaign(
        workspace,
        "unreadable",
        {
            "version": 1,
            "name": "unreadable",
            "members": [
                {"id": "lib", "path": "lib"},
                {"id": "broken", "path": "broken"},
            ],
        },
    )
    assert work_cmd.ready(target=workspace, campaign="unreadable", parallel_safe=True, json_output=True) == 1
    unreadable = json.loads(capsys.readouterr().out)
    assert unreadable["reason"] == campaign_mod.REASON_UNREADABLE_MEMBER
    assert "ready" not in unreadable
    assert "waves" not in unreadable


def test_campaign_parallel_safe_reports_member_graph_degrade_keeps_file_waves(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lib = _member_repo(workspace, "lib")
    app = _member_repo(workspace, "app")
    _clock(monkeypatch, minute=45)
    db = lib / ".graphtrail" / "graphtrail.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"fake-graphtrail-index")
    monkeypatch.setattr(partition_mod.component_bins, "resolve", lambda _name: "/fake/graphtrail")
    lib_task = _add(lib, "Lib graph member")
    app_task = _add(app, "App file-only member")
    _set_pending_footprint(lib, lib_task["id"], files=["src/shared.py"])
    _set_pending_footprint(app, app_task["id"], files=["src/shared.py"])
    _write_campaign(
        workspace,
        "degrade",
        {
            "version": 1,
            "name": "degrade",
            "members": [
                {"id": "lib", "path": "lib"},
                {"id": "app", "path": "app"},
            ],
        },
    )

    assert work_cmd.ready(target=workspace, campaign="degrade", parallel_safe=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["wave_count"] == 1
    assert payload["waves"][0]["task_ids"] == [
        campaign_mod.qualify_task_id("lib", lib_task["id"]),
        campaign_mod.qualify_task_id("app", app_task["id"]),
    ]
    assert payload["partition_degraded"] is True
    assert "app:" in payload["partition_degraded_reason"]
    assert "graphtrail index not found" in payload["partition_degraded_reason"]
    by_member = {item["member_id"]: item for item in payload["member_partitions"]}
    assert by_member["lib"]["partition_degraded"] is False
    assert by_member["lib"]["partition_mode"] == partition_mod.MODE_FILE_OVERLAP_SYMBOL_IMPACT
    assert by_member["app"]["partition_degraded"] is True
    assert by_member["app"]["partition_mode"] == partition_mod.MODE_FILE_OVERLAP
    assert payload["partition_mode"] == partition_mod.MODE_FILE_OVERLAP


def test_campaign_parallel_safe_leaves_per_repo_output_byte_unchanged(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lib = _member_repo(workspace, "lib")
    app = _member_repo(workspace, "app")
    _clock(monkeypatch, minute=50)
    lib_a = _add(lib, "Per-repo alpha")
    lib_b = _add(lib, "Per-repo beta")
    app_task = _add(app, "Other member")
    _set_pending_footprint(lib, lib_a["id"], files=["src/alpha.py"])
    _set_pending_footprint(lib, lib_b["id"], files=["src/alpha.py"])
    _set_pending_footprint(app, app_task["id"], files=["src/alpha.py"])

    assert work_cmd.ready(target=lib, parallel_safe=True, json_output=True) == 0
    before = capsys.readouterr().out
    before_payload = json.loads(before)
    assert "campaign" not in before_payload
    assert before_payload["wave_count"] == 2
    assert before_payload["waves"][0]["task_ids"] == [lib_a["id"]]
    assert before_payload["waves"][1]["task_ids"] == [lib_b["id"]]

    _write_campaign(
        workspace,
        "compare",
        {
            "version": 1,
            "name": "compare",
            "members": [
                {"id": "lib", "path": "lib"},
                {"id": "app", "path": "app"},
            ],
        },
    )
    assert work_cmd.ready(target=workspace, campaign="compare", parallel_safe=True, json_output=True) == 0
    campaign_payload = json.loads(capsys.readouterr().out)
    assert campaign_payload["wave_count"] == 2
    assert campaign_payload["waves"][0]["task_ids"] == [
        campaign_mod.qualify_task_id("lib", lib_a["id"]),
        campaign_mod.qualify_task_id("app", app_task["id"]),
    ]

    assert work_cmd.ready(target=lib, parallel_safe=True, json_output=True) == 0
    after = capsys.readouterr().out
    assert after == before
