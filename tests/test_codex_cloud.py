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


def test_status_scan_reads_bracket_token_first():
    # real installed-CLI format: [STATUS] <task title>, then repo/age lines
    assert codex_cloud._scan_status("[PENDING] fix failed tests\nbrigade  -  12s ago\nno diff") is None
    assert codex_cloud._scan_status("[COMPLETED] fix failed tests\nbrigade  -  2m ago") == "completed"
    assert codex_cloud._scan_status("[FAILED] harmless title\nbrigade") == "failed"
    # bracket token is authoritative even if prose elsewhere says otherwise
    assert codex_cloud._scan_status("[RUNNING] task\nnote: previous attempt failed") is None


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


def test_run_agent_threads_process_registry_to_codex_cloud(monkeypatch):
    registry = proc.ProcessRegistry()
    seen = {}

    def fake_cloud_task(prompt, *, env_id, timeout, cwd=None, process_registry=None):
        seen.update(
            prompt=prompt,
            env_id=env_id,
            timeout=timeout,
            cwd=cwd,
            process_registry=process_registry,
        )
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command)
    monkeypatch.setattr(codex_cloud, "run_cloud_task", fake_cloud_task)

    result = agents.run_agent("codex-cloud:env-123", "fix it", process_registry=registry)

    assert result.ok
    assert seen["process_registry"] is registry


def test_run_agent_preserves_legacy_codex_cloud_call_shape(monkeypatch):
    def fake_cloud_task(prompt, *, env_id, timeout, cwd=None):
        return agents.AgentResult(text=f"{env_id}: {prompt}", ok=True)

    monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command)
    monkeypatch.setattr(codex_cloud, "run_cloud_task", fake_cloud_task)

    assert agents.run_agent("codex-cloud:env-123", "fix it").ok


def test_parse_task_id_variants():
    assert (
        codex_cloud.parse_task_id("Created https://chatgpt.com/codex/tasks/task_e_abc123 for review") == "task_e_abc123"
    )
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
    fake = FakeRuns(
        {
            "exec": [_result(stdout="Submitted https://chatgpt.com/codex/tasks/task_ok1")],
            "status": [
                _result(stdout="status: queued"),
                _result(stdout="status: running"),
                _result(stdout="status: completed"),
            ],
            "diff": [_result(stdout="diff --git a/f b/f\n+fixed")],
        }
    )
    monkeypatch.setattr(codex_cloud.proc, "run", fake)
    result = codex_cloud.run_cloud_task(
        "fix it",
        env_id="env-123",
        timeout=600,
        cwd=Path("."),
        poll_interval=0,
        sleep=lambda s: None,
        clock=lambda: 0,
    )
    assert result.ok
    assert result.thread_id == "task_ok1"
    assert result.status == "completed"
    assert "diff --git" in result.text
    assert "NOT applied locally" in result.text
    assert fake.calls[0][:5] == ["codex", "cloud", "exec", "--env", "env-123"]


def test_run_cloud_task_threads_registry_through_submit_status_and_diff(monkeypatch):
    registry = proc.ProcessRegistry()
    calls = []

    def fake_run(argv, timeout=30.0, env=None, cwd=None, process_registry=None):
        calls.append((argv[2], process_registry))
        if argv[2] == "exec":
            return _result(stdout="task_registered1")
        if argv[2] == "status":
            return _result(stdout="status: completed")
        return _result(stdout="")

    monkeypatch.setattr(codex_cloud.proc, "run", fake_run)

    result = codex_cloud.run_cloud_task(
        "fix it",
        env_id="env-123",
        timeout=600,
        poll_interval=0,
        sleep=lambda seconds: None,
        clock=lambda: 0,
        process_registry=registry,
    )

    assert result.ok
    assert calls == [("exec", registry), ("status", registry), ("diff", registry)]


def test_run_cloud_task_failure_status(monkeypatch):
    fake = FakeRuns(
        {
            "exec": [_result(stdout="task_bad1")],
            "status": [_result(stdout="status: failed (environment setup)")],
        }
    )
    monkeypatch.setattr(codex_cloud.proc, "run", fake)
    result = codex_cloud.run_cloud_task(
        "x",
        env_id="e",
        timeout=600,
        poll_interval=0,
        sleep=lambda s: None,
        clock=lambda: 0,
    )
    assert not result.ok
    assert result.status == "failed"
    assert "task_bad1" in result.detail


def test_run_cloud_task_submit_error(monkeypatch):
    fake = FakeRuns(
        {
            "exec": [_result(code=1, stderr="Error: no cloud environments are available")],
        }
    )
    monkeypatch.setattr(codex_cloud.proc, "run", fake)
    result = codex_cloud.run_cloud_task(
        "x",
        env_id="e",
        timeout=600,
        sleep=lambda s: None,
        clock=lambda: 0,
    )
    assert not result.ok
    assert "no cloud environments" in result.detail


def test_run_cloud_task_timeout_reports_task_id(monkeypatch):
    fake = FakeRuns(
        {
            "exec": [_result(stdout="task_slow1")],
            "status": [_result(stdout="status: running")],
        }
    )
    monkeypatch.setattr(codex_cloud.proc, "run", fake)
    ticks = iter([0, 1, 700, 701, 702])
    result = codex_cloud.run_cloud_task(
        "x",
        env_id="e",
        timeout=600,
        poll_interval=0,
        sleep=lambda s: None,
        clock=lambda: next(ticks),
    )
    assert not result.ok
    assert result.status == "timeout"
    assert result.timed_out is True
    assert result.failure_kind == "timeout"
    assert "task_slow1" in result.detail


@pytest.mark.parametrize("timed_out_stage", ["submit", "status", "diff"])
def test_run_cloud_task_command_timeout_is_terminal_timeout(monkeypatch, timed_out_stage):
    timed_out_subcommand = "exec" if timed_out_stage == "submit" else timed_out_stage
    fake = FakeRuns(
        {
            "exec": [_result(code=124, stderr="timeout after 5s")]
            if timed_out_subcommand == "exec"
            else [_result(stdout="task_timeout1")],
            "status": [_result(code=124, stderr="timeout after 5s")]
            if timed_out_subcommand == "status"
            else [_result(stdout="status: completed")],
            "diff": [_result(code=124, stderr="timeout after 5s")]
            if timed_out_subcommand == "diff"
            else [_result(stdout="")],
        }
    )
    monkeypatch.setattr(codex_cloud.proc, "run", fake)
    ticks = iter([0, 0, 601])

    result = codex_cloud.run_cloud_task(
        "x",
        env_id="e",
        timeout=600,
        poll_interval=0,
        sleep=lambda seconds: None,
        clock=lambda: next(ticks, 601),
    )

    assert result.ok is False
    assert result.status == "timeout"
    assert result.timed_out is True
    assert result.failure_kind == "timeout"
    assert timed_out_stage in result.detail


def test_run_cloud_task_polls_past_incidental_failure_words(monkeypatch):
    fake = FakeRuns(
        {
            "exec": [_result(stdout="task_title1")],
            "status": [
                _result(stdout="Task: fix failed tests\nStatus: running"),
                _result(stdout="Task: fix failed tests\nStatus: completed"),
            ],
            "diff": [_result(stdout="")],
        }
    )
    monkeypatch.setattr(codex_cloud.proc, "run", fake)
    result = codex_cloud.run_cloud_task(
        "fix failed tests",
        env_id="e",
        timeout=600,
        poll_interval=0,
        sleep=lambda s: None,
        clock=lambda: 0,
    )
    assert result.ok
    assert result.status == "completed"
    assert "No diff produced" in result.text


def test_run_cloud_task_surfaces_diff_failure(monkeypatch):
    fake = FakeRuns(
        {
            "exec": [_result(stdout="task_dferr1")],
            "status": [_result(stdout="Status: completed")],
            "diff": [_result(code=1, stderr="network unreachable")],
        }
    )
    monkeypatch.setattr(codex_cloud.proc, "run", fake)
    result = codex_cloud.run_cloud_task(
        "x",
        env_id="e",
        timeout=600,
        poll_interval=0,
        sleep=lambda s: None,
        clock=lambda: 0,
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
        "x",
        env_id="e",
        timeout=600,
        poll_interval=0,
        sleep=lambda s: None,
        clock=lambda: next(ticks),
    )
    status_timeouts = [t for sub, t in captured if sub == "status"]
    assert all(t <= codex_cloud.POLL_TIMEOUT for t in status_timeouts)
    assert min(status_timeouts) == 5.0  # capped to the remaining-time floor


def test_run_cloud_task_unparseable_submit_output(monkeypatch):
    fake = FakeRuns(
        {
            "exec": [_result(stdout="some prose without any identifier tokens here")],
        }
    )
    monkeypatch.setattr(codex_cloud.proc, "run", fake)
    result = codex_cloud.run_cloud_task(
        "x",
        env_id="e",
        timeout=600,
        sleep=lambda s: None,
        clock=lambda: 0,
    )
    assert not result.ok
    assert "could not parse" in result.detail
