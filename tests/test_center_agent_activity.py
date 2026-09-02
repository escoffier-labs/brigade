import json
from datetime import datetime, timedelta, timezone

from brigade import center_cmd, grokbot_jobs
from brigade.center_cmd import agent_activity as activity_records
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
                {"worker": "worker-a", "task": "private worker prompt"},
                {"worker": "worker-b", "task": "private worker prompt"},
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
    assert {record["task_label"] for record in workers} == {"Worker assignment"}
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


def test_center_activity_projects_grokbot_queue_without_private_instructions(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    job_id = grokbot_jobs.enqueue(
        tmp_path,
        {
            "label": "Center Grok Bot tracker",
            "role": "implementation-worker",
            "repository": "example/brigade",
            "base_ref": "main",
            "ownership_paths": ["src/brigade/cloud_tracker.py"],
            "instructions": "PRIVATE CENTER INSTRUCTIONS TOKEN=do-not-display",
            "verification_commands": ["pytest -q tests/test_center_agent_activity.py"],
            "artifact": {"kind": "draft-pr"},
            "timeout_seconds": 900,
        },
        "center-grokbot-tracker",
        now=now,
    )["job_id"]
    home = tmp_path / "empty-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))

    records = activity_records.collect(tmp_path, now=now)
    row = next(record for record in records if record["links"].get("task_id") == job_id)
    assert row["provider"] == "grokbot"
    assert row["harness"] == "grokbot-cloud"
    assert row["state"] == "running"
    assert "PRIVATE CENTER INSTRUCTIONS" not in json.dumps(records)


def test_center_activity_marks_merged_grokbot_draft_pr_succeeded(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    job_id = grokbot_jobs.enqueue(
        tmp_path,
        {
            "label": "Merged Grok Bot tracker",
            "role": "implementation-worker",
            "repository": "example/brigade",
            "base_ref": "main",
            "ownership_paths": ["src/brigade/cloud_tracker.py"],
            "instructions": "PRIVATE MERGED INSTRUCTIONS",
            "verification_commands": ["pytest -q tests/test_center_agent_activity.py"],
            "artifact": {"kind": "draft-pr"},
            "timeout_seconds": 900,
        },
        "merged-grokbot-tracker",
        now=now,
    )["job_id"]
    grokbot_jobs.claim(tmp_path, job_id, "bot-a", "lease-a", 600, now=now)
    grokbot_jobs.transition(tmp_path, job_id, "bot-a", "lease-a", "running", now=now)
    grokbot_jobs.transition(
        tmp_path,
        job_id,
        "bot-a",
        "lease-a",
        "completed",
        now=now,
        artifact={
            "kind": "draft-pr",
            "branch": "grokbot/merged-grokbot-tracker",
            "url": "https://github.com/example/brigade/pull/456",
        },
    )
    monkeypatch.setattr(
        "brigade.cloud_tracker.observe_github",
        lambda _: {
            "branches": [],
            "prs": [
                {
                    "head": "grokbot/merged-grokbot-tracker",
                    "state": "MERGED",
                    "url": "https://github.com/example/brigade/pull/456",
                    "isDraft": True,
                    "headRefOid": "c" * 40,
                }
            ],
        },
    )
    home = tmp_path / "empty-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))

    records = activity_records.collect(tmp_path, now=now + timedelta(hours=30))
    row = next(record for record in records if record["links"].get("task_id") == job_id)
    assert row["classification"] == "landed"
    assert row["state"] == "succeeded"


def test_session_discovery_bounds_directory_walks(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    for index in range(300):
        (root / f"session-{index:03}").mkdir(parents=True, exist_ok=True)
    actual_scandir = activity_records.os.scandir
    calls = []

    def tracked_scandir(path):
        calls.append(path)
        return actual_scandir(path)

    monkeypatch.setattr(activity_records.os, "scandir", tracked_scandir)

    assert activity_records._bounded_session_paths(root, "codex") == []
    assert len(calls) == 200


def test_cursor_session_discovery_prioritizes_transcript_directories(tmp_path):
    root = tmp_path / "projects"
    for index in range(300):
        (root / f"project-{index:03}").mkdir(parents=True, exist_ok=True)
    transcript = root / "project-000" / "agent-transcripts" / "active"
    transcript.mkdir(parents=True)
    (transcript / "session.jsonl").write_text("private transcript")

    paths = activity_records._bounded_session_paths(root, "cursor")

    assert paths == [transcript / "session.jsonl"]


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
                    "host": "alpha",
                    "label": "Brigade run",
                    "task_label": "Build activity view",
                    "model": "gpt-5.6-terra",
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

    assert 'class="machine-card"' in fragment
    assert 'data-host="alpha"' in fragment
    assert 'class="agent-tile"' in fragment
    assert "● running" in fragment
    assert "Build activity view" in fragment
    assert "gpt-5.6-terra" in fragment
    assert "Br" in fragment  # brigade-run monogram
    assert "private-id" in fragment.split("<details>", 1)[1]
    assert "private-id" not in fragment.split("<details>", 1)[0]
    assert "Table" in fragment
    assert 'class="data-table"' in fragment


def test_agent_activity_view_groups_hosts_nests_children_and_collapses_old_completed():
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    parent = {
        "activity_id": "brigade:run:parent",
        "parent_activity_id": None,
        "provider": "brigade",
        "harness": "brigade-run",
        "kind": "run",
        "host": "alpha",
        "label": "Brigade run",
        "task_label": "Parent task",
        "model": None,
        "state": "running",
        "started_at": now.isoformat(),
        "last_updated_at": now.isoformat(),
        "elapsed_seconds": 30,
        "source": {"name": "brigade-run-journal", "authority": "authoritative"},
        "links": {},
    }
    child = {
        **parent,
        "activity_id": "brigade:worker:parent:coder",
        "parent_activity_id": "brigade:run:parent",
        "kind": "worker",
        "label": "coder",
        "task_label": "Child assignment",
        "provider": "cursor",
        "harness": "cursor-agent",
        "model": "grok-4.5",
    }
    old_done = {
        **parent,
        "activity_id": "brigade:run:old",
        "task_label": "Finished hours ago",
        "state": "succeeded",
        "started_at": (now - timedelta(hours=5)).isoformat(),
        "last_updated_at": (now - timedelta(hours=4)).isoformat(),
        "elapsed_seconds": 3600,
    }
    fragment = agent_activity.render(
        {
            "agent_activity_summary": [],
            "agent_activity": [parent, child, old_done],
            "completed_window_seconds": 3600,
            "default_hosts": ["alpha", "beta", "gamma"],
            "host_kinds": {"alpha": "workstation", "beta": "gpu", "gamma": "desktop", "cloud": "cloud"},
            "local_host": "alpha",
        },
        "test-nonce",
    )

    assert fragment.index("Parent task") < fragment.index("Finished hours ago")
    assert 'class="agent-tile agent-tile-child"' in fragment
    assert "show 1 older" in fragment.lower() or "Show 1 older" in fragment
    assert 'data-host="cloud"' in fragment
    assert "cloud tracking not wired" in fragment.lower()
    assert "#890" in fragment
    assert 'data-mark="cursor"' in fragment
    assert "<svg" in fragment
    for host in ("alpha", "beta", "gamma", "cloud"):
        assert f'data-host="{host}"' in fragment


def test_humanize_cwd_folder_name_strips_date_prefix():
    assert (
        activity_records._humanize_folder_name("2026-08-11-you-are-a-fresh-frontier-conductor")
        == "you are a fresh frontier conductor"
    )


def test_codex_session_extracts_task_label_from_cwd_folder(tmp_path, capsys, monkeypatch):
    home = tmp_path / "home"
    folder = "2026-08-11-you-are-a-fresh-frontier-conductor"
    codex = home / ".codex" / "sessions" / "2026" / "08" / "12"
    codex.mkdir(parents=True)
    session = codex / "rollout-safe-session.jsonl"
    session.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "cwd": f"/tmp/worktrees/{folder}",
                    "model_provider": "openai",
                },
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "turn_context",
                "payload": {"cwd": f"/tmp/worktrees/{folder}", "model": "gpt-5.6-terra"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "secret private prompt body"},
            }
        )
        + "\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    _write_json(
        tmp_path / ".brigade" / "center" / "agent-activity-sources.json",
        {"local_host": "workbench"},
    )

    assert center_cmd.activity(target=tmp_path, json_output=True) == 0
    records = json.loads(capsys.readouterr().out)["agent_activity"]
    codex_row = next(record for record in records if record["provider"] == "codex" and record["kind"] == "agent")

    assert codex_row["task_label"] == "you are a fresh frontier conductor"
    assert codex_row["model"] == "gpt-5.6-terra"
    assert codex_row["host"] == "workbench"
    assert "secret private prompt body" not in json.dumps(records)
    assert "/tmp/" not in json.dumps(records)


def test_codex_session_falls_back_to_first_user_prompt_when_cwd_is_generic(tmp_path, monkeypatch):
    home = tmp_path / "home"
    codex = home / ".codex" / "sessions" / "2026" / "08" / "12"
    codex.mkdir(parents=True)
    (codex / "rollout-prompt.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"cwd": "/tmp/repos/brigade"}})
        + "\n"
        + json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "Revise the agent activity view layout"},
            }
        )
        + "\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))

    records = activity_records.collect(tmp_path)
    codex_row = next(record for record in records if record["provider"] == "codex" and record["kind"] == "agent")
    assert codex_row["task_label"] == "Revise the agent activity view layout"


def test_external_sources_redact_journal_text_and_missing_local_sources_are_stale(tmp_path, capsys, monkeypatch):
    home = tmp_path / "empty-home"
    journal = tmp_path / "fleet" / "t3.jsonl"
    journal.parent.mkdir()
    journal.write_text(
        json.dumps(
            {
                "label": "private prompt TOKEN=secret",
                "task_label": "/home/private/project",  # content-guard: allow home-path
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
    assert t3["task_label"] == "Unknown task"
    assert t3["parent_activity_id"] is None
    assert {record["provider"] for record in records} >= {"codex", "cursor"}
    assert all(record["state"] == "stale" for record in records if record["kind"] == "source")
    assert "TOKEN=secret" not in json.dumps(records)
    assert all(record.get("task_label") != "Task unavailable" for record in records)


def test_agent_activity_view_docstring_answers_operator_question():
    assert "right now" in agent_activity.__doc__.lower()
    assert "fleet" in agent_activity.__doc__.lower()


def test_agent_activity_view_summary_strip_counts_states_and_blocked_hosts():
    now = datetime.now(timezone.utc).isoformat()
    records = [
        _view_record(activity_id="a1", state="running", host="alpha", last_updated_at=now, started_at=now),
        _view_record(activity_id="a2", state="running", host="beta", last_updated_at=now, started_at=now),
        _view_record(activity_id="a3", state="blocked", host="beta", last_updated_at=now, started_at=now),
        _view_record(activity_id="a4", state="totally-bogus", host="gamma", last_updated_at=now, started_at=now),
        _view_record(activity_id="a5", state=None, host="gamma", last_updated_at=now, started_at=now),
    ]
    fragment = agent_activity.render({"agent_activity": records}, "test-nonce")

    strip = fragment.split('class="page-summary"', 1)[1].split("</div>", 1)[0]
    assert "5 agents tracked" in strip
    assert "2 running" in strip
    assert "1 blocked on beta" in strip
    assert "2 unknown" in strip
    board_index = fragment.index('class="machine-board"')
    summary_index = fragment.index('class="page-summary"')
    assert summary_index < board_index


def test_agent_activity_view_summary_strip_handles_empty_records():
    fragment = agent_activity.render({"agent_activity": []}, "test-nonce")
    strip = fragment.split('class="page-summary"', 1)[1].split("</div>", 1)[0]
    assert "No agent activity recorded right now." in strip


def test_agent_activity_view_state_legend_lists_plain_words_in_sync_with_icons():
    fragment = agent_activity.render({"agent_activity": []}, "test-nonce")

    assert 'class="state-legend"' in fragment
    for state, icon in agent_activity._STATE_ICON.items():
        assert f"{icon} {state}" in fragment
        entry = f'<span class="legend-entry">{icon} {state}</span>'
        assert entry in fragment


def test_agent_activity_view_count_chips_carry_plain_words_not_icon_only():
    now = datetime.now(timezone.utc).isoformat()
    records = [
        _view_record(activity_id="a1", state="running", last_updated_at=now, started_at=now),
        _view_record(activity_id="a2", state="running", last_updated_at=now, started_at=now),
    ]
    fragment = agent_activity.render({"agent_activity": records}, "test-nonce")

    assert "● running 2" in fragment


def test_agent_activity_view_count_chips_collapse_unknown_states_like_summary():
    # A bogus state and a real "unknown" on the same host must fold into a
    # single "unknown" chip (bucketed via _bucket_state), matching the summary
    # strip, not render two indistinguishable split-count chips.
    now = datetime.now(timezone.utc).isoformat()
    records = [
        _view_record(activity_id="b1", state="totally-bogus", last_updated_at=now, started_at=now),
        _view_record(activity_id="b2", state="unknown", last_updated_at=now, started_at=now),
    ]
    fragment = agent_activity.render({"agent_activity": records}, "test-nonce")

    # The count strip collapses both into one "unknown 2" chip; before the fix
    # they split into two "unknown 1" chips. (The raw state may still appear in
    # the per-agent detail row's State column, which is correct.)
    assert "unknown 2" in fragment
    assert "unknown 1" not in fragment


def _view_record(**overrides):
    now = datetime.now(timezone.utc)
    record = {
        "activity_id": "brigade:run:demo",
        "parent_activity_id": None,
        "provider": "brigade",
        "harness": "brigade-run",
        "kind": "run",
        "host": "alpha",
        "label": "Brigade run",
        "task_label": "Demo task",
        "model": "gpt-5.6-terra",
        "state": "running",
        "started_at": now.isoformat(),
        "last_updated_at": now.isoformat(),
        "elapsed_seconds": 60,
        "source": {"name": "brigade-run-journal", "authority": "authoritative"},
        "links": {},
    }
    record.update(overrides)
    return record


def test_provider_badges_render_inline_svg_marks_not_monograms():
    now = datetime.now(timezone.utc).isoformat()
    records = [
        _view_record(
            activity_id="cursor:1",
            provider="cursor",
            harness="cursor-agent",
            task_label="Cursor lane",
            model="grok-4.5",
            last_updated_at=now,
            started_at=now,
        ),
        _view_record(
            activity_id="codex:1",
            provider="codex",
            harness="codex-cli",
            task_label="Codex lane",
            model="gpt-5.6",
            last_updated_at=now,
            started_at=now,
        ),
        _view_record(
            activity_id="claude:1",
            provider="claude",
            harness="claude-code",
            task_label="Claude lane",
            model="opus-4.8",
            last_updated_at=now,
            started_at=now,
        ),
        _view_record(
            activity_id="t3:1",
            provider="t3",
            harness="t3-code",
            task_label="T3 lane",
            model="t3-code",
            last_updated_at=now,
            started_at=now,
        ),
        _view_record(
            activity_id="brigade:1",
            provider="brigade",
            harness="brigade-run",
            task_label="Brigade lane",
            model="terra",
            last_updated_at=now,
            started_at=now,
        ),
    ]
    fragment = agent_activity.render({"agent_activity": records}, "test-nonce")

    assert 'class="provider-glyph"' in fragment
    for mark in ("cursor", "codex", "claude", "t3"):
        assert f'data-mark="{mark}"' in fragment
        tile = fragment.split(f'data-provider="{mark}"', 1)[1].split('data-provider="', 1)[0]
        assert "<svg" in tile
        assert "<img" not in tile
    assert ">Cu<" not in fragment
    assert ">Cx<" not in fragment
    assert ">Cl<" not in fragment
    assert ">Br<" in fragment
    assert "#d97757" in fragment.lower()
    assert "grok-4.5" in fragment
    assert "gpt-5.6" in fragment
    assert "opus-4.8" in fragment
    assert "http://" not in fragment
    assert "https://" not in fragment


def test_zombie_running_run_without_lock_renders_stale_last_seen(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    home = tmp_path / "empty-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    started = now - timedelta(hours=45)
    run = tmp_path / ".brigade" / "runs" / "zombie-run"
    _write_json(
        run / "run.json",
        {
            "task_label": "Zombie dispatch",
            "orchestrator": "planner",
            "status": "running",
            "started_at": started.isoformat(),
            "status_started_at": started.isoformat(),
            "active_seats": ["worker-a"],
        },
    )
    _write_json(run / "plan.json", {"assignments": [{"worker": "worker-a", "task_label": "Stuck worker"}]})

    records = activity_records.collect(tmp_path, now=now)
    run_record = next(record for record in records if record["kind"] == "run")
    worker = next(record for record in records if record["kind"] == "worker")
    assert run_record["state"] == "stale"
    assert worker["state"] == "stale"

    fragment = agent_activity.render({"agent_activity": records}, "test-nonce")
    board = fragment.split('class="machine-board"', 1)[1]
    assert "● running" not in board
    assert "⌛ stale (last seen 45h ago)" in board


def test_blocked_45h_run_is_older_history_not_live(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    home = tmp_path / "empty-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    started = now - timedelta(hours=45)
    run = tmp_path / ".brigade" / "runs" / "blocked-old"
    _write_json(
        run / "run.json",
        {
            "task_label": "Ancient blocked run",
            "orchestrator": "planner",
            "status": "blocked",
            "started_at": started.isoformat(),
            "status_started_at": started.isoformat(),
        },
    )

    records = activity_records.collect(tmp_path, now=now)
    run_record = next(record for record in records if record["kind"] == "run")
    assert run_record["state"] == "stale"

    fragment = agent_activity.render({"agent_activity": records}, "test-nonce")
    board = fragment.split('class="machine-board"', 1)[1]
    live_section = fragment.split('class="completed-expander"', 1)[0]
    assert "Ancient blocked run" not in live_section or "older" in fragment.lower()
    assert "show " in fragment.lower() and "older" in fragment.lower()
    assert "! blocked" not in board
    collapsed_html = (
        fragment.split('class="completed-expander"', 1)[1] if 'class="completed-expander"' in fragment else ""
    )
    assert "Ancient blocked run" in collapsed_html


def test_recency_partition_hides_older_than_12h_and_floats_attention():
    now = datetime.now(timezone.utc)
    live_old = _view_record(
        activity_id="live-old",
        task_label="Needs attention still",
        state="failed",
        started_at=(now - timedelta(hours=30)).isoformat(),
        last_updated_at=(now - timedelta(hours=30)).isoformat(),
    )
    recent_done = _view_record(
        activity_id="recent-done",
        task_label="Finished two hours ago",
        state="succeeded",
        started_at=(now - timedelta(hours=2)).isoformat(),
        last_updated_at=(now - timedelta(hours=2)).isoformat(),
    )
    old_done = _view_record(
        activity_id="old-done",
        task_label="Finished yesterday",
        state="succeeded",
        started_at=(now - timedelta(hours=20)).isoformat(),
        last_updated_at=(now - timedelta(hours=20)).isoformat(),
    )
    fragment = agent_activity.render(
        {"agent_activity": [old_done, recent_done, live_old]},
        "test-nonce",
    )
    live_section = fragment.split('class="completed-expander"', 1)[0]
    collapsed = fragment.split('class="completed-expander"', 1)[1]
    assert "Needs attention still" in live_section
    assert "Finished two hours ago" in live_section
    assert "Finished yesterday" not in live_section
    assert "Finished yesterday" in collapsed
    assert "show 1 older" in fragment.lower()
    assert live_section.index("Needs attention still") < live_section.index("Finished two hours ago")


def test_live_lock_keeps_recent_running_run(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    home = tmp_path / "empty-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setattr("brigade.runguard.run_lock_state", lambda workspace, run_dir: "live")
    started = now - timedelta(minutes=4)
    run = tmp_path / ".brigade" / "runs" / "live-run"
    _write_json(
        run / "run.json",
        {
            "task_label": "Live dispatch",
            "orchestrator": "planner",
            "status": "running",
            "started_at": started.isoformat(),
            "status_started_at": started.isoformat(),
        },
    )

    records = activity_records.collect(tmp_path, now=now)
    run_record = next(record for record in records if record["kind"] == "run")
    assert run_record["state"] == "running"


def test_heartbeat_over_two_hours_is_stale_even_with_live_lock(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    home = tmp_path / "empty-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setattr("brigade.runguard.run_lock_state", lambda workspace, run_dir: "live")
    started = now - timedelta(hours=3)
    run = tmp_path / ".brigade" / "runs" / "quiet-run"
    _write_json(
        run / "run.json",
        {
            "task_label": "Silent lock",
            "orchestrator": "planner",
            "status": "running",
            "started_at": started.isoformat(),
            "status_started_at": started.isoformat(),
        },
    )

    records = activity_records.collect(tmp_path, now=now)
    run_record = next(record for record in records if record["kind"] == "run")
    assert run_record["state"] == "stale"


def test_agent_activity_view_dedupes_cloud_when_in_default_hosts():
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    record = {
        "activity_id": "brigade:run:cloudy",
        "parent_activity_id": None,
        "provider": "brigade",
        "harness": "brigade-run",
        "kind": "run",
        "host": "cloud",
        "label": "Brigade run",
        "task_label": "Cloud task",
        "model": None,
        "state": "running",
        "started_at": now.isoformat(),
        "last_updated_at": now.isoformat(),
        "elapsed_seconds": 30,
        "source": {"name": "brigade-run-journal", "authority": "authoritative"},
        "links": {},
    }
    fragment = agent_activity.render(
        {
            "agent_activity_summary": [],
            "agent_activity": [record],
            "completed_window_seconds": 3600,
            "default_hosts": ["cloud"],
            "host_kinds": {"cloud": "cloud"},
            "local_host": "workbench",
        },
        "test-nonce",
    )

    assert fragment.count('data-host="cloud"') == 1
