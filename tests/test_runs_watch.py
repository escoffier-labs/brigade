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
    assert records[2]["worker"] == "coder"
    assert records[2]["event"]["method"] == "item/started"
    assert records[-1]["status"] == "ok"


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
