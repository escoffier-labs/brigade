import json
from datetime import datetime, timedelta, timezone

from brigade import center_cmd
from brigade.center_cmd.dashboard.views import agent_activity


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def test_center_activity_v2_lists_run_workers_and_stale_fleet_observation(tmp_path, capsys):
    now = datetime.now(timezone.utc)
    run = tmp_path / ".brigade" / "runs" / "run-one"
    _write_json(
        run / "run.json",
        {
            "task": "A prompt that must not enter the activity payload",
            "task_label": "Operator view",
            "orchestrator": "planner",
            "status": "dispatching",
            "started_at": (now - timedelta(minutes=3)).isoformat(),
            "active_seats": ["worker-a", "worker-b"],
        },
    )
    _write_json(
        run / "plan.json",
        {
            "assignments": [
                {"worker": "worker-a", "task": "private worker prompt", "task_label": "Build contract"},
                {"worker": "worker-b", "task": "private worker prompt", "task_label": "Build view"},
            ]
        },
    )
    _write_json(run / "worker-results.json", {"results": [{"worker": "worker-b", "ok": True, "status": "completed"}]})
    _write_json(
        tmp_path / ".brigade" / "center" / "agent-activity-sources.json",
        {"sources": [{"provider": "t3", "host": "fleet-east", "journal": "fleet/missing.jsonl"}]},
    )

    assert center_cmd.activity(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["activity_envelope_version"] == 2
    assert payload["schema"]["version"] == 2
    records = payload["agent_activity"]
    run_record = next(record for record in records if record["kind"] == "run")
    workers = [record for record in records if record["kind"] == "worker"]
    assert len(workers) == 2
    assert all(record["parent_activity_id"] == run_record["activity_id"] for record in workers)
    assert {record["task_label"] for record in workers} == {"Build contract", "Build view"}
    assert next(record for record in workers if record["label"] == "worker-b")["state"] == "succeeded"
    fleet = next(record for record in records if record["provider"] == "t3")
    assert fleet["host"] == "fleet-east"
    assert fleet["state"] == "stale"
    assert fleet["source"]["authority"] == "best-effort"
    assert all("/home/" not in json.dumps(record) for record in records)
    assert all("private worker prompt" not in json.dumps(record) for record in records)


def test_center_activity_observes_codex_and_cursor_session_files_without_transcripts(tmp_path, capsys, monkeypatch):
    home = tmp_path / "home"
    codex = home / ".codex" / "sessions" / "2026" / "08" / "12"
    cursor = home / ".cursor" / "projects" / "project" / "agent-transcripts" / "session-one"
    codex.mkdir(parents=True)
    cursor.mkdir(parents=True)
    (codex / "rollout-safe-session.jsonl").write_text('{"type":"response_item","payload":{"text":"private prompt"}}\n')
    (cursor / "session-one.jsonl").write_text('{"role":"user","message":"private transcript"}\n')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))

    assert center_cmd.activity(target=tmp_path, json_output=True) == 0
    records = json.loads(capsys.readouterr().out)["agent_activity"]

    assert {record["provider"] for record in records} >= {"codex", "cursor"}
    assert all(
        record["state"] in {"running", "stale"} for record in records if record["provider"] in {"codex", "cursor"}
    )
    rendered = json.dumps(records)
    assert "private prompt" not in rendered
    assert "private transcript" not in rendered


def test_agent_activity_view_uses_state_words_and_keeps_ids_in_details():
    fragment = agent_activity.render(
        {
            "agent_activity_summary": [{"provider": "brigade", "state_counts": {"running": 1}}],
            "agent_activity": [
                {
                    "activity_id": "brigade:run:private-id",
                    "parent_activity_id": None,
                    "provider": "brigade",
                    "harness": "brigade-run",
                    "kind": "run",
                    "host": "local",
                    "label": "Brigade run",
                    "task_label": "Build activity view",
                    "state": "running",
                    "started_at": "2026-08-12T12:00:00+00:00",
                    "last_updated_at": "2026-08-12T12:01:00+00:00",
                    "elapsed_seconds": 60,
                    "source": {"name": "brigade-run-journal", "authority": "authoritative"},
                    "links": {"run": "brigade runs show private-id"},
                }
            ],
        },
        "test-nonce",
    )

    assert "Provider summary" in fragment
    assert "● running" in fragment
    assert "Build activity view" in fragment
    assert "private-id" in fragment.split("<details>", 1)[1]
    assert "private-id" not in fragment.split("<details>", 1)[0]


def test_external_sources_redact_journal_text_and_missing_local_sources_are_stale(tmp_path, capsys, monkeypatch):
    home = tmp_path / "empty-home"
    journal = tmp_path / "fleet" / "t3.jsonl"
    journal.parent.mkdir()
    journal.write_text(
        json.dumps(
            {
                "label": "private prompt TOKEN=secret",
                "task_label": "/home/private/project",
                "parent_activity_id": "private-parent",
                "state": "running",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        + "\n"
    )
    _write_json(
        tmp_path / ".brigade" / "center" / "agent-activity-sources.json",
        {"sources": [{"provider": "t3", "host": "fleet-east", "journal": "fleet/t3.jsonl"}]},
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))

    assert center_cmd.activity(target=tmp_path, json_output=True) == 0
    records = json.loads(capsys.readouterr().out)["agent_activity"]

    t3 = next(record for record in records if record["provider"] == "t3")
    assert t3["label"] == "t3 agent"
    assert t3["task_label"] == "Task unavailable"
    assert t3["parent_activity_id"] is None
    assert {record["provider"] for record in records} >= {"codex", "cursor"}
    assert all(record["state"] == "stale" for record in records if record["kind"] == "source")
    assert "TOKEN=secret" not in json.dumps(records)
