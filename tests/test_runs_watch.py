import json
import shlex

import pytest

from brigade import cli
from brigade import run_journal
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


def _assert_inspect_command_well_formed(inspect_command, *, run_id, run_dir):
    """The suggested command must be a safe, single-token `runs show <run_id>`."""
    argv = shlex.split(inspect_command)
    assert argv == ["brigade", "runs", "show", run_id]
    assert inspect_command == f"brigade runs show {run_id}"
    assert str(run_dir) not in inspect_command
    assert "<" not in inspect_command and ">" not in inspect_command
    assert "/" not in argv[-1] and "\\" not in argv[-1]


def test_watch_json_stale_lock_inspect_command_uses_run_id(tmp_path, capsys):
    run_dir = tmp_path / ".brigade" / "runs" / "20260817-100000-watch-bbbbbb"
    leaks = _plant_watch_secrets(run_dir, tmp_path, status="failed", failure_phase="stale-lock-recovery")

    assert runs_cmd.watch(run_dir, cwd=tmp_path, interval=0.0, json_output=True) == 1

    out = capsys.readouterr().out
    _assert_no_planted_secrets(out, leaks)
    summary = next(json.loads(line) for line in out.splitlines() if json.loads(line).get("type") == "summary")
    assert summary["run_id"] == run_dir.name
    _assert_inspect_command_well_formed(summary["inspect_command"], run_id=run_dir.name, run_dir=run_dir)


def test_watch_json_stale_lock_inspect_command_is_runnable(tmp_path, capsys, monkeypatch):
    """Regression: emitting `runs show <run_id>` was unrunnable before show resolved ids.

    The #958 path redaction swapped the absolute run path for `run_id` but left
    `inspect_command` pointing at `runs show`, which previously required a
    filesystem path. Executing the emitted argv from the workspace cwd would
    have caught that: rc 2 / `run directory not found: <run_id>`.
    """
    run_dir = tmp_path / ".brigade" / "runs" / "20260817-100000-watch-bbbbbb"
    leaks = _plant_watch_secrets(run_dir, tmp_path, status="failed", failure_phase="stale-lock-recovery")

    assert runs_cmd.watch(run_dir, cwd=tmp_path, interval=0.0, json_output=True) == 1
    out = capsys.readouterr().out
    summary = next(json.loads(line) for line in out.splitlines() if json.loads(line).get("type") == "summary")
    inspect_command = summary["inspect_command"]
    _assert_inspect_command_well_formed(inspect_command, run_id=run_dir.name, run_dir=run_dir)
    _assert_no_planted_secrets(inspect_command, leaks)

    argv = shlex.split(inspect_command)
    monkeypatch.chdir(tmp_path)
    rc = cli.main(argv[1:])
    captured = capsys.readouterr()
    assert "run directory not found" not in captured.err
    assert rc == 1
    assert "status: failed" in captured.out
    assert "failure phase: stale-lock-recovery" in captured.out


def test_runs_show_cli_resolves_bare_run_id_and_path(tmp_path, capsys):
    run_dir = tmp_path / ".brigade" / "runs" / "20260817-100000-watch-dddddd"
    _plant_watch_secrets(run_dir, tmp_path, status="failed", failure_phase="stale-lock-recovery")

    assert cli.main(["runs", "show", run_dir.name, "--cwd", str(tmp_path)]) == 1
    by_id = capsys.readouterr()
    assert "run directory not found" not in by_id.err
    assert "failure phase: stale-lock-recovery" in by_id.out

    assert cli.main(["runs", "show", str(run_dir)]) == 1
    by_path = capsys.readouterr()
    assert "run directory not found" not in by_path.err
    assert "failure phase: stale-lock-recovery" in by_path.out


def _append_watch_lifecycle(run_dir, event_type, payload):
    journal = run_dir / "events" / "lifecycle.jsonl"
    previous = len(run_journal.read_journal_bounded(journal).events)
    return run_journal.append_event(
        journal,
        run_id=run_dir.name,
        event_type=event_type,
        payload=payload,
        idempotency_key=f"watch-fact-{previous + 1}",
        expected_previous_sequence=previous,
        recorded_at="2026-10-28T12:00:00.000000Z",
    )


@pytest.mark.parametrize(
    "event_type,status,expected",
    [
        ("run.failed", "failed", 1),
        ("run.failed", "timeout", 1),
        ("run.interrupted", "canceled", 1),
        ("run.orphaned", "orphaned", 1),
        ("run.completed", "ok", 0),
        ("run.completed", "completed", 0),
    ],
)
@pytest.mark.parametrize("json_output", [False, True])
def test_watch_terminates_on_durable_outcome_with_stale_snapshot(
    tmp_path, capsys, monkeypatch, event_type, status, expected, json_output
):
    run_dir = tmp_path / "stale-outcome"
    _write_run(run_dir, status="dispatching")
    _append_watch_lifecycle(run_dir, "run.created", {"status": "started"})
    _append_watch_lifecycle(run_dir, event_type, {"status": status})
    journal = run_dir / "events" / "lifecycle.jsonl"
    original = (run_dir / "run.json").read_bytes(), journal.read_bytes()

    def unexpected_sleep(_seconds):
        pytest.fail("watch polled again after a durable terminal outcome")

    monkeypatch.setattr(runs_cmd.time, "sleep", unexpected_sleep)
    assert runs_cmd.watch(run_dir, cwd=tmp_path, interval=0, json_output=json_output) == expected
    output = capsys.readouterr().out
    if json_output:
        records = [json.loads(line) for line in output.splitlines()]
        assert next(row for row in records if row["type"] == "run")["status"] == status
        summaries = [row for row in records if row["type"] == "summary"]
        assert len(summaries) == 1
        assert summaries[0]["status"] == status
    else:
        assert f"status: {status}" in output
        assert f"summary: {status}" in output
    assert original == ((run_dir / "run.json").read_bytes(), journal.read_bytes())


@pytest.mark.parametrize("json_output", [False, True])
def test_watch_waits_through_reopen_until_new_terminal_event(tmp_path, capsys, monkeypatch, json_output):
    run_dir = tmp_path / "reopened"
    _write_run(run_dir, status="failed", finished=True)
    _append_watch_lifecycle(run_dir, "run.created", {"status": "started"})
    _append_watch_lifecycle(run_dir, "run.failed", {"status": "failed"})
    _append_watch_lifecycle(run_dir, "run.recovery.started", {"detail": "synthetic reopen"})
    snapshot = (run_dir / "run.json").read_bytes()
    sleeps = []

    def finish_reopened_run(_seconds):
        sleeps.append(True)
        assert len(sleeps) == 1, "watch ignored the new durable outcome"
        _append_watch_lifecycle(run_dir, "run.failed", {"status": "incomplete"})

    monkeypatch.setattr(runs_cmd.time, "sleep", finish_reopened_run)
    assert runs_cmd.watch(run_dir, cwd=tmp_path, interval=0, json_output=json_output) == 1
    assert len(sleeps) == 1
    output = capsys.readouterr().out
    if json_output:
        records = [json.loads(line) for line in output.splitlines()]
        assert [row["status"] for row in records if row["type"] == "run"] == ["running", "incomplete"]
        assert [row["status"] for row in records if row["type"] == "summary"] == ["incomplete"]
    else:
        assert "status: running" in output
        assert "summary: incomplete" in output
        assert "summary: failed" not in output
    assert snapshot == (run_dir / "run.json").read_bytes()


def _read_partial_tail_watch(run_dir, mode):
    if mode == "stream":
        return None, list(runs_cmd.iter_watch_records(run_dir, interval=0))
    return runs_cmd.watch(run_dir, cwd=run_dir.parent, interval=0, json_output=mode == "json"), None


@pytest.mark.parametrize("mode", ["json", "stream"])
def test_watch_reports_partial_terminal_journal_without_success_summary(tmp_path, capsys, monkeypatch, mode):
    run_dir = tmp_path / "partial"
    _write_run(run_dir, status="ok", finished=True)
    _append_watch_lifecycle(run_dir, "run.completed", {"status": "ok"})
    journal = run_dir / "events" / "lifecycle.jsonl"
    journal.write_bytes(journal.read_bytes() + b'{"partial":')

    original = journal.read_bytes()
    sleeps = []

    monkeypatch.setattr(runs_cmd.time, "sleep", sleeps.append)
    rc, records = _read_partial_tail_watch(run_dir, mode)
    if mode != "stream":
        assert rc == 2
    captured = capsys.readouterr()
    assert "lifecycle journal" in captured.err
    if records is None:
        records = [json.loads(line) for line in captured.out.splitlines()]
    assert not any(row["type"] in {"run", "summary"} for row in records)
    assert sleeps == [0.01, 0.01]
    assert journal.read_bytes() == original


@pytest.mark.parametrize("mode,finishes_on", [("text", 1), ("json", 1), ("stream", 1), ("json", 2), ("stream", 2)])
def test_watch_waits_for_transient_partial_journal_tail(tmp_path, capsys, monkeypatch, mode, finishes_on):
    run_dir = tmp_path / "partial-append"
    _write_run(run_dir, status="dispatching")
    _append_watch_lifecycle(run_dir, "run.failed", {"status": "failed"})
    journal = run_dir / "events" / "lifecycle.jsonl"
    complete = journal.read_bytes()
    journal.write_bytes(complete[: len(complete) // 2])
    snapshot = (run_dir / "run.json").read_bytes()
    sleeps = []

    def finish_append(seconds):
        assert seconds == 0.01, "watch entered normal polling instead of completing the terminal outcome"
        sleeps.append(seconds)
        assert len(sleeps) <= finishes_on
        if len(sleeps) == finishes_on:
            journal.write_bytes(complete)

    monkeypatch.setattr(runs_cmd.time, "sleep", finish_append)
    rc, records = _read_partial_tail_watch(run_dir, mode)
    if mode != "stream":
        assert rc == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    if mode == "text":
        assert "status: failed" in captured.out
        assert "summary: failed" in captured.out
        assert "status: dispatching" not in captured.out
    else:
        if records is None:
            records = [json.loads(line) for line in captured.out.splitlines()]
        assert [row["status"] for row in records if row["type"] == "run"] == ["failed"]
        assert [row["status"] for row in records if row["type"] == "summary"] == ["failed"]
    assert sleeps == [0.01] * finishes_on
    assert snapshot == (run_dir / "run.json").read_bytes()
    assert journal.read_bytes() == complete


@pytest.mark.parametrize("mode", ["json", "stream"])
@pytest.mark.parametrize("fault", ["missing", "empty", "corrupt", "io"])
def test_watch_fails_closed_if_partial_journal_changes_during_retry(tmp_path, capsys, monkeypatch, mode, fault):
    run_dir = tmp_path / "changing-partial"
    _write_run(run_dir, status="ok", finished=True)
    _append_watch_lifecycle(run_dir, "run.completed", {"status": "ok"})
    journal = run_dir / "events" / "lifecycle.jsonl"
    journal.write_bytes(journal.read_bytes() + b'{"partial":')
    sleeps = []

    def change_journal(seconds):
        sleeps.append(seconds)
        assert sleeps == [0.01], "watch retried a journal disappearance, corruption or I/O failure"
        if fault == "missing":
            journal.unlink()
        elif fault == "empty":
            journal.write_bytes(b"")
        elif fault == "corrupt":
            journal.write_bytes(b"not-json\n")
        else:

            def unreadable(_path):
                raise OSError("synthetic journal read failure")

            monkeypatch.setattr(run_journal, "read_journal_bounded", unreadable)

    monkeypatch.setattr(runs_cmd.time, "sleep", change_journal)
    rc, records = _read_partial_tail_watch(run_dir, mode)
    if mode != "stream":
        assert rc == 2
    captured = capsys.readouterr()
    assert "lifecycle journal" in captured.err
    if records is None:
        records = [json.loads(line) for line in captured.out.splitlines()]
    assert not any(row["type"] in {"run", "summary"} for row in records)
    assert sleeps == [0.01]


@pytest.mark.parametrize("mode", ["json", "stream"])
@pytest.mark.parametrize("retry_number", [1, 2])
@pytest.mark.parametrize("fault", ["truncated", "replaced"])
def test_watch_partial_retry_preserves_each_verified_prefix(tmp_path, capsys, monkeypatch, mode, retry_number, fault):
    run_dir = tmp_path / "partial-prefix"
    _write_run(run_dir, status="ok", finished=True)
    _append_watch_lifecycle(run_dir, "run.created", {"status": "started"})
    _append_watch_lifecycle(run_dir, "run.failed", {"status": "failed"})
    journal = run_dir / "events" / "lifecycle.jsonl"
    prefix = journal.read_bytes()
    _append_watch_lifecycle(run_dir, "run.dispatch.completed", {"seat": "worker", "attempt": 1})
    advanced = journal.read_bytes()
    replacement = tmp_path / "replacement" / "events" / "lifecycle.jsonl"
    for sequence in range(1, retry_number + 2):
        run_journal.append_event(
            replacement,
            run_id=run_dir.name,
            event_type="run.created",
            payload={"status": "started"},
            idempotency_key=f"replacement-{sequence}",
            expected_previous_sequence=sequence - 1,
            recorded_at="2026-10-28T12:00:00.000000Z",
        )
    journal.write_bytes(prefix + b'{"partial":')
    sleeps = []

    def replace_prefix(seconds):
        assert seconds == 0.01
        sleeps.append(seconds)
        assert len(sleeps) <= retry_number
        if len(sleeps) < retry_number:
            journal.write_bytes(advanced + b'{"partial":')
        elif fault == "replaced":
            journal.write_bytes(replacement.read_bytes())
        else:
            journal.write_bytes(prefix if retry_number == 2 else prefix.splitlines(keepends=True)[0])

    monkeypatch.setattr(runs_cmd.time, "sleep", replace_prefix)
    rc, records = _read_partial_tail_watch(run_dir, mode)
    if mode != "stream":
        assert rc == 2
    captured = capsys.readouterr()
    assert "lifecycle journal" in captured.err
    if records is None:
        records = [json.loads(line) for line in captured.out.splitlines()]
    assert not any(row["type"] in {"run", "summary"} for row in records)
    assert sleeps == [0.01] * retry_number


@pytest.mark.parametrize("reader", ["show", "detail", "inspect"])
def test_static_readers_do_not_retry_partial_journal(tmp_path, capsys, monkeypatch, reader):
    run_dir = tmp_path / "static-partial"
    _write_run(run_dir, status="ok", finished=True)
    _append_watch_lifecycle(run_dir, "run.completed", {"status": "ok"})
    journal = run_dir / "events" / "lifecycle.jsonl"
    journal.write_bytes(journal.read_bytes() + b'{"partial":')

    def unexpected_sleep(_seconds):
        pytest.fail("a static reader retried a partial journal")

    monkeypatch.setattr(runs_cmd.time, "sleep", unexpected_sleep)
    if reader == "detail":
        payload, diagnostic, rc = runs_cmd.run_detail_contract(run_dir)
        assert payload is None
        assert "partial trailing record" in diagnostic
    else:
        rc = runs_cmd.show(run_dir) if reader == "show" else runs_cmd.inspect(run_dir, cwd=tmp_path)
        assert "partial trailing record" in capsys.readouterr().err
    assert rc == 2
