import json

from brigade import cli
from brigade import runs_cmd


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _write_run(run_dir, *, status="started", transport="app-server", finished=False):
    payload = {
        "task": "watch the run",
        "cwd": "/tmp/example",
        "orchestrator": "chef",
        "dry_run": False,
        "read_only": False,
        "status": status,
        "started_at": "2026-07-08T10:00:00Z",
        "artifacts": str(run_dir),
        "codex_transport": transport,
    }
    if finished:
        payload["finished_at"] = "2026-07-08T10:00:03Z"
        payload["duration_seconds"] = 3.0
    _write_json(run_dir / "run.json", payload)


def _finish_run(run_dir, *, status="ok"):
    _write_run(run_dir, status=status, finished=True)
    _write_json(
        run_dir / "worker-results.json",
        {"results": [{"worker": "coder", "task": "implement", "ok": True, "detail": "", "text": "done"}]},
    )
    _write_json(
        run_dir / "synthesis.json",
        {"orchestrator": "chef", "result": {"ok": True, "detail": "", "text": "final answer"}},
    )
    (run_dir / "final.txt").write_text("final answer\n")


def _append_event(run_dir, worker, method, item_type="commandExecution"):
    events = run_dir / "events"
    events.mkdir(exist_ok=True)
    with (events / f"{worker}.jsonl").open("a") as fh:
        fh.write(
            json.dumps(
                {
                    "method": method,
                    "params": {
                        "threadId": "thread-1",
                        "item": {"id": "item-1", "type": item_type},
                    },
                }
            )
            + "\n"
        )


def test_watch_terminal_run_prints_final_summary(tmp_path, capsys):
    run_dir = tmp_path / "run"
    _finish_run(run_dir)

    assert runs_cmd.watch(run_dir, cwd=tmp_path, interval=0.0) == 0

    out = capsys.readouterr().out
    assert f"watching: {run_dir}" in out
    assert "status: ok" in out
    assert "workers:" in out
    assert "  [ok] coder" in out
    assert "final:" in out
    assert "  final answer" in out
    assert "summary: ok in 3s" in out


def test_watch_tails_events_and_stops_after_mid_run_completion(tmp_path, capsys, monkeypatch):
    run_dir = tmp_path / "run"
    _write_run(run_dir)
    _write_json(run_dir / "plan.json", {"assignments": [{"stage": 1, "worker": "coder", "task": "implement"}]})
    _append_event(run_dir, "coder", "item/started")
    slept = {"count": 0}

    def fake_sleep(_seconds):
        slept["count"] += 1
        _append_event(run_dir, "coder", "item/completed")
        _finish_run(run_dir)

    monkeypatch.setattr(runs_cmd.time, "sleep", fake_sleep)

    assert runs_cmd.watch(run_dir, cwd=tmp_path, interval=0.01) == 0

    out = capsys.readouterr().out
    assert slept["count"] == 1
    assert "plan:" in out
    assert "  stage 1 -> coder: implement" in out
    assert "event: coder item/started commandExecution" in out
    assert "event: coder item/completed commandExecution" in out
    assert "summary: ok in 3s" in out


def test_watch_json_outputs_ndjson_incrementally(tmp_path, capsys, monkeypatch):
    run_dir = tmp_path / "run"
    _write_run(run_dir)
    _append_event(run_dir, "coder", "item/started")

    def fake_sleep(_seconds):
        _append_event(run_dir, "coder", "turn/completed", "turn")
        _finish_run(run_dir)

    monkeypatch.setattr(runs_cmd.time, "sleep", fake_sleep)

    assert runs_cmd.watch(run_dir, cwd=tmp_path, interval=0.01, json_output=True) == 0

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [record["type"] for record in records] == [
        "watch",
        "run",
        "event",
        "run",
        "event",
        "workers",
        "synthesis",
        "final",
        "summary",
    ]
    assert all(record["schema"] == "brigade.run-watch.v1" for record in records)
    assert records[0]["run_id"] == run_dir.name
    assert "run" not in records[0]
    assert records[2]["worker"] == "coder"
    assert records[2]["event"]["method"] == "item/started"
    assert records[2]["event"]["item_type"] == "commandExecution"
    assert "params" not in records[2]["event"]
    assert records[-1]["status"] == "ok"
    assert records[-1]["run_id"] == run_dir.name
    assert "run" not in records[-1]


def test_watch_exec_transport_without_events_dir_still_finishes(tmp_path, capsys, monkeypatch):
    run_dir = tmp_path / "run"
    _write_run(run_dir, transport="exec")

    def fake_sleep(_seconds):
        _finish_run(run_dir)

    monkeypatch.setattr(runs_cmd.time, "sleep", fake_sleep)

    assert runs_cmd.watch(run_dir, cwd=tmp_path, interval=0.01) == 0

    captured = capsys.readouterr()
    assert "summary: ok in 3s" in captured.out
    assert "events directory not found" not in captured.err


def test_watch_cli_resolves_run_name_with_runs_dir(tmp_path, capsys):
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "run-1"
    _finish_run(run_dir)

    assert cli.main(["runs", "watch", "run-1", "--cwd", str(tmp_path), "--runs-dir", str(runs_root)]) == 0

    assert f"watching: {run_dir.resolve()}" in capsys.readouterr().out


_FAKE_TOKEN = "tok-SECRET-123"
_PROMPT_BODY = "FULL PROMPT BODY\nsecond prompt line"
_STDOUT_BODY = "RAW STDOUT\nsecond stdout line"


def _secret_path(tmp_path):
    return tmp_path / "operator-home" / ".secrets" / "api.token"


def _plant_watch_secrets(run_dir, tmp_path, *, status="ok", failure_phase=None):
    secret_path = _secret_path(tmp_path)
    payload = {
        "task": "watch the run",
        "cwd": str(secret_path),
        "log_path": str(secret_path / "run.log"),
        "auth_token": _FAKE_TOKEN,
        "prompt": _PROMPT_BODY,
        "transcript": _PROMPT_BODY,
        "stdout": _STDOUT_BODY,
        "stderr": _STDOUT_BODY,
        "env": {"OPENAI_API_KEY": _FAKE_TOKEN},
        "orchestrator": "chef",
        "dry_run": False,
        "read_only": False,
        "status": status,
        "started_at": "2026-07-08T10:00:00Z",
        "finished_at": "2026-07-08T10:00:02Z",
        "duration_seconds": 2.0,
        "artifacts": str(run_dir),
        "codex_transport": "app-server",
        "control_transport": {"owner_token": _FAKE_TOKEN},
    }
    if failure_phase is not None:
        payload["failure_phase"] = failure_phase
        payload["failure"] = {"phase": failure_phase, "kind": "stale-lock", "detail": "stale lock remains"}
    _write_json(run_dir / "run.json", payload)
    _write_json(
        run_dir / "plan.json",
        {"assignments": [{"stage": 1, "worker": "coder", "task": "implement"}]},
    )
    _write_json(
        run_dir / "worker-results.json",
        {
            "results": [
                {
                    "worker": "coder",
                    "task": "implement",
                    "ok": True,
                    "detail": "",
                    "text": _PROMPT_BODY,
                    "stdout": _STDOUT_BODY,
                    "stdout_log": str(secret_path / "worker.stdout.log"),
                    "stderr_log": str(secret_path / "worker.stderr.log"),
                }
            ]
        },
    )
    _write_json(
        run_dir / "synthesis.json",
        {"orchestrator": "chef", "result": {"ok": True, "detail": "", "text": _PROMPT_BODY}},
    )
    (run_dir / "final.txt").write_text("final answer\n")
    events = run_dir / "events"
    events.mkdir(exist_ok=True)
    (events / "coder.jsonl").write_text(
        json.dumps(
            {
                "method": "agent/message",
                "params": {
                    "auth_token": _FAKE_TOKEN,
                    "cwd": str(secret_path),
                    "log_path": str(secret_path / "run.log"),
                    "prompt": _PROMPT_BODY,
                    "stdout": _STDOUT_BODY,
                    "item": {"id": "item-1", "type": "commandExecution"},
                },
            }
        )
        + "\n"
    )
    return {
        "token": _FAKE_TOKEN,
        "secret_path": str(secret_path),
        "prompt": _PROMPT_BODY,
        "stdout": _STDOUT_BODY,
        "run_dir": str(run_dir),
        "tmp_path": str(tmp_path),
    }


_SECRET_FRAGMENTS = (_FAKE_TOKEN, "FULL PROMPT BODY", "RAW STDOUT")


def _assert_no_planted_secrets(serialized, leaks):
    for label, needle in leaks.items():
        assert needle not in serialized, f"{label} leaked into contract payload"
    for fragment in _SECRET_FRAGMENTS:
        assert fragment not in serialized, f"fragment {fragment!r} leaked into contract payload"


def test_watch_json_omits_planted_token_path_and_prompt(tmp_path, capsys):
    run_dir = tmp_path / ".brigade" / "runs" / "20260817-100000-watch-aaaaaa"
    leaks = _plant_watch_secrets(run_dir, tmp_path)

    assert runs_cmd.watch(run_dir, cwd=tmp_path, interval=0.0, json_output=True) == 0

    out = capsys.readouterr().out
    records = [json.loads(line) for line in out.splitlines()]
    assert records
    assert all(record["schema"] == "brigade.run-watch.v1" for record in records)
    _assert_no_planted_secrets(out, leaks)
    for record in records:
        _assert_no_planted_secrets(json.dumps(record), leaks)
        assert "auth_token" not in json.dumps(record)
        assert "params" not in json.dumps(record) or record.get("type") != "event"
    watch_record = next(record for record in records if record["type"] == "watch")
    assert watch_record["run_id"] == run_dir.name
    event_record = next(record for record in records if record["type"] == "event")
    assert event_record["event"] == {"item_type": "commandExecution", "method": "agent/message"}
    summary = next(record for record in records if record["type"] == "summary")
    assert summary["run_id"] == run_dir.name
    assert "run" not in summary


def test_watch_json_stale_lock_inspect_command_uses_run_id(tmp_path, capsys):
    run_dir = tmp_path / ".brigade" / "runs" / "20260817-100000-watch-bbbbbb"
    leaks = _plant_watch_secrets(run_dir, tmp_path, status="failed", failure_phase="stale-lock-recovery")

    assert runs_cmd.watch(run_dir, cwd=tmp_path, interval=0.0, json_output=True) == 1

    out = capsys.readouterr().out
    _assert_no_planted_secrets(out, leaks)
    summary = next(json.loads(line) for line in out.splitlines() if json.loads(line).get("type") == "summary")
    assert summary["run_id"] == run_dir.name
    assert summary["inspect_command"] == f"brigade runs show {run_dir.name}"
    assert str(run_dir) not in summary["inspect_command"]
