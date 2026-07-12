from pathlib import Path

import pytest

from brigade import agents, codex_cloud, proc


def test_codex_cloud_ref_is_known_and_maps_to_codex():
    assert agents.is_known("codex-cloud:env-123")
    assert agents.command_for("codex-cloud:env-123") == "codex"
    assert agents.read_only_enforcement("codex-cloud:env-123") == "hard"


def test_bare_codex_cloud_ref_is_not_known():
    assert not agents.is_known("codex-cloud:")


def test_status_scan_ignores_incidental_words_outside_status_lines():
    text = "Task: fix failed tests in parser\nStatus: running\n"
    assert codex_cloud._scan_status(text) is None
    text = "Task: fix failed tests in parser\nStatus: completed\n"
    assert codex_cloud._scan_status(text) == "completed"
    # no status-shaped line at all: fall back to whole-text scan
    assert codex_cloud._scan_status("the run has finished") == "finished"


def test_build_argv_rejects_codex_cloud():
    with pytest.raises(ValueError, match="run_agent"):
        agents.build_argv("codex-cloud:env-123", "hi")


def test_run_agent_requires_env_id(monkeypatch):
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    result = agents.run_agent("codex-cloud:", "hi")
    assert not result.ok
    assert "environment id" in result.detail


def test_run_agent_rejects_model_pin(monkeypatch):
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    result = agents.run_agent("codex-cloud:env-123", "hi", model="gpt-5.5")
    assert not result.ok
    assert "model" in result.detail


def test_parse_task_id_variants():
    assert codex_cloud.parse_task_id(
        "Created https://chatgpt.com/codex/tasks/task_e_abc123 for review"
    ) == "task_e_abc123"
    assert codex_cloud.parse_task_id("Task ID: 9f8e7d6c5b") == "9f8e7d6c5b"
    assert codex_cloud.parse_task_id("submitted task_abc-42 ok") == "task_abc-42"
    assert codex_cloud.parse_task_id("task_xyz9\n") == "task_xyz9"
    assert codex_cloud.parse_task_id("no ids in this prose at all") is None


class FakeRuns:
    """Queue of proc.Result objects keyed by the subcommand (exec/status/diff)."""

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []

    def __call__(self, argv, timeout=30.0, env=None, cwd=None):
        sub = argv[2]  # codex cloud <sub>
        self.calls.append(argv)
        queue = self.script[sub]
        return queue.pop(0) if len(queue) > 1 else queue[0]


def _result(code=0, stdout="", stderr=""):
    return proc.Result(code=code, stdout=stdout, stderr=stderr)


def test_run_cloud_task_happy_path(monkeypatch):
    fake = FakeRuns({
        "exec": [_result(stdout="Submitted https://chatgpt.com/codex/tasks/task_ok1")],
        "status": [
            _result(stdout="status: queued"),
            _result(stdout="status: running"),
            _result(stdout="status: completed"),
        ],
        "diff": [_result(stdout="diff --git a/f b/f\n+fixed")],
    })
    monkeypatch.setattr(codex_cloud.proc, "run", fake)
    result = codex_cloud.run_cloud_task(
        "fix it", env_id="env-123", timeout=600, cwd=Path("."),
        poll_interval=0, sleep=lambda s: None, clock=lambda: 0,
    )
    assert result.ok
    assert result.thread_id == "task_ok1"
    assert result.status == "completed"
    assert "diff --git" in result.text
    assert "NOT applied locally" in result.text
    assert fake.calls[0][:5] == ["codex", "cloud", "exec", "--env", "env-123"]


def test_run_cloud_task_failure_status(monkeypatch):
    fake = FakeRuns({
        "exec": [_result(stdout="task_bad1")],
        "status": [_result(stdout="status: failed (environment setup)")],
    })
    monkeypatch.setattr(codex_cloud.proc, "run", fake)
    result = codex_cloud.run_cloud_task(
        "x", env_id="e", timeout=600, poll_interval=0, sleep=lambda s: None, clock=lambda: 0,
    )
    assert not result.ok
    assert result.status == "failed"
    assert "task_bad1" in result.detail


def test_run_cloud_task_submit_error(monkeypatch):
    fake = FakeRuns({
        "exec": [_result(code=1, stderr="Error: no cloud environments are available")],
    })
    monkeypatch.setattr(codex_cloud.proc, "run", fake)
    result = codex_cloud.run_cloud_task(
        "x", env_id="e", timeout=600, sleep=lambda s: None, clock=lambda: 0,
    )
    assert not result.ok
    assert "no cloud environments" in result.detail


def test_run_cloud_task_timeout_reports_task_id(monkeypatch):
    fake = FakeRuns({
        "exec": [_result(stdout="task_slow1")],
        "status": [_result(stdout="status: running")],
    })
    monkeypatch.setattr(codex_cloud.proc, "run", fake)
    ticks = iter([0, 1, 700, 701, 702])
    result = codex_cloud.run_cloud_task(
        "x", env_id="e", timeout=600, poll_interval=0,
        sleep=lambda s: None, clock=lambda: next(ticks),
    )
    assert not result.ok
    assert result.status == "pending"
    assert "task_slow1" in result.detail


def test_run_cloud_task_polls_past_incidental_failure_words(monkeypatch):
    fake = FakeRuns({
        "exec": [_result(stdout="task_title1")],
        "status": [
            _result(stdout="Task: fix failed tests\nStatus: running"),
            _result(stdout="Task: fix failed tests\nStatus: completed"),
        ],
        "diff": [_result(stdout="")],
    })
    monkeypatch.setattr(codex_cloud.proc, "run", fake)
    result = codex_cloud.run_cloud_task(
        "fix failed tests", env_id="e", timeout=600,
        poll_interval=0, sleep=lambda s: None, clock=lambda: 0,
    )
    assert result.ok
    assert result.status == "completed"
    assert "No diff produced" in result.text


def test_run_cloud_task_surfaces_diff_failure(monkeypatch):
    fake = FakeRuns({
        "exec": [_result(stdout="task_dferr1")],
        "status": [_result(stdout="Status: completed")],
        "diff": [_result(code=1, stderr="network unreachable")],
    })
    monkeypatch.setattr(codex_cloud.proc, "run", fake)
    result = codex_cloud.run_cloud_task(
        "x", env_id="e", timeout=600, poll_interval=0, sleep=lambda s: None, clock=lambda: 0,
    )
    assert result.ok
    assert "WARNING" in result.text
    assert "network unreachable" in result.text


def test_run_cloud_task_caps_poll_timeout_to_remaining(monkeypatch):
    captured = []

    def fake_run(argv, timeout=30.0, env=None, cwd=None):
        captured.append((argv[2], timeout))
        if argv[2] == "exec":
            return _result(stdout="task_cap1")
        if argv[2] == "status":
            return _result(stdout="Status: completed")
        return _result(stdout="")

    monkeypatch.setattr(codex_cloud.proc, "run", fake_run)
    ticks = iter([0, 595, 596, 597, 598])  # 5s left when polling starts
    codex_cloud.run_cloud_task(
        "x", env_id="e", timeout=600, poll_interval=0,
        sleep=lambda s: None, clock=lambda: next(ticks),
    )
    status_timeouts = [t for sub, t in captured if sub == "status"]
    assert all(t <= codex_cloud.POLL_TIMEOUT for t in status_timeouts)
    assert min(status_timeouts) == 5.0  # capped to the remaining-time floor


def test_run_cloud_task_unparseable_submit_output(monkeypatch):
    fake = FakeRuns({
        "exec": [_result(stdout="some prose without any identifier tokens here")],
    })
    monkeypatch.setattr(codex_cloud.proc, "run", fake)
    result = codex_cloud.run_cloud_task(
        "x", env_id="e", timeout=600, sleep=lambda s: None, clock=lambda: 0,
    )
    assert not result.ok
    assert "could not parse" in result.detail
