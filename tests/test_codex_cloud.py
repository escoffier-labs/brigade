import json
from pathlib import Path

import pytest

from brigade import agents, cli, cloud_tracker, codex_cloud, fleet_client, proc


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
    assert codex_cloud.parse_task_id("Task ID: task_9f8e7d6c5b") == "task_9f8e7d6c5b"
    assert codex_cloud.parse_task_id("submitted task_abc-42 ok") == "task_abc-42"
    assert codex_cloud.parse_task_id("task_xyz9\n") == "task_xyz9"
    assert codex_cloud.parse_task_id("no ids in this prose at all") is None


def test_parse_task_id_never_falls_back_to_arbitrary_token():
    assert codex_cloud.parse_task_id("just-a-token") is None
    assert codex_cloud.parse_task_id("abc123") is None
    assert codex_cloud.parse_task_id("task_") is None
    assert codex_cloud.parse_task_id("Task ID: 9f8e7d6c5b") is None


class FakeRuns:
    """Queue of proc.Result objects keyed by the subcommand (exec/status/diff/list)."""

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []

    def __call__(self, argv, timeout=30.0, env=None, cwd=None, process_registry=None):
        sub = argv[2]  # codex cloud <sub>
        self.calls.append(argv)
        queue = self.script[sub]
        return queue.pop(0) if len(queue) > 1 else queue[0]


def _result(code=0, stdout="", stderr=""):
    return proc.Result(code=code, stdout=stdout, stderr=stderr)


def _grant_hub(monkeypatch, *, lease_id: str | None = "lease-1", granted: bool = True, reason: str = "ok"):
    """Wire fleet_client so admission succeeds; return the recorded calls."""
    recorded: dict[str, list] = {"admit": [], "bind": [], "renew": [], "release": []}

    def fake_admit(*args, **kwargs):
        recorded["admit"].append({"args": args, "kwargs": kwargs})
        return fleet_client.CloudDecision(granted=granted, reason=reason, lease={"lease_id": lease_id}, holder="h1")

    def fake_bind(*args, **kwargs):
        recorded["bind"].append({"args": args, "kwargs": kwargs})
        return fleet_client.CloudDecision(granted=True, reason="ok")

    def fake_renew(*args, **kwargs):
        recorded["renew"].append({"args": args, "kwargs": kwargs})
        return fleet_client.CloudDecision(granted=True, reason="ok")

    def fake_release(*args, **kwargs):
        recorded["release"].append({"args": args, "kwargs": kwargs})
        return fleet_client.CloudDecision(granted=True, reason="ok")

    monkeypatch.setattr(fleet_client, "admit_cloud", fake_admit)
    monkeypatch.setattr(fleet_client, "bind_cloud", fake_bind)
    monkeypatch.setattr(fleet_client, "renew_cloud", fake_renew)
    monkeypatch.setattr(fleet_client, "release_cloud", fake_release)
    monkeypatch.setattr(codex_cloud, "_hub_configured", lambda: True)
    monkeypatch.setattr(codex_cloud, "_repo_from_cwd", lambda cwd: "owner/repo")
    return recorded


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
    assert result.thread_id == "task_bad1"
    assert result.detail == "cloud task failed"
    assert "environment setup" not in result.detail


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
    assert result.detail == "cloud submit failed"
    assert "no cloud environments" not in result.detail
    assert result.failure_kind == "provider-error"


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
    assert result.thread_id == "task_slow1"
    assert result.detail == "cloud task timed out"
    assert "task_slow1" not in result.detail


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
    assert "WARNING: cloud diff failed" in result.text
    assert "network unreachable" not in result.text


def test_run_cloud_task_caps_poll_timeout_to_remaining(monkeypatch):
    captured = []

    def fake_run(argv, timeout=30.0, env=None, cwd=None, process_registry=None):
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
    assert result.detail == "cloud submit returned no usable task id"


class TestFleetAdmission:
    def test_admission_uses_canonical_codex_hub_provider(self, monkeypatch):
        recorded = _grant_hub(monkeypatch)
        fake = FakeRuns(
            {
                "exec": [_result(stdout="task_hubkey1")],
                "status": [_result(stdout="Status: completed")],
                "diff": [_result(stdout="")],
            }
        )
        monkeypatch.setattr(codex_cloud.proc, "run", fake)

        result = codex_cloud.run_cloud_task(
            "fix it", env_id="e", timeout=600, poll_interval=0, sleep=lambda s: None, clock=lambda: 0
        )

        assert result.ok
        assert recorded["admit"][0]["args"][0] == "codex"

    def test_admission_ordering_and_lifecycle(self, monkeypatch):
        recorded = _grant_hub(monkeypatch)
        fake = FakeRuns(
            {
                "exec": [_result(stdout="task_ok1")],
                "status": [
                    _result(stdout="status: queued"),
                    _result(stdout="status: running"),
                    _result(stdout="status: completed"),
                ],
                "diff": [_result(stdout="")],
            }
        )
        monkeypatch.setattr(codex_cloud.proc, "run", fake)
        result = codex_cloud.run_cloud_task(
            "fix it",
            env_id="e",
            timeout=600,
            poll_interval=0,
            sleep=lambda s: None,
            clock=lambda: 0,
        )
        assert result.ok
        assert result.thread_id == "task_ok1"
        assert recorded["admit"]
        assert fake.calls[0][:3] == ["codex", "cloud", "exec"]
        assert recorded["bind"]
        assert recorded["bind"][0]["args"][0] == "lease-1"
        assert recorded["bind"][0]["kwargs"]["provider_task_id"] == "task_ok1"
        # Two non-terminal status polls (queued, running) before completed.
        assert len(recorded["renew"]) == 2
        assert recorded["release"]
        assert recorded["release"][0]["args"][0] == "lease-1"
        assert recorded["release"][0]["kwargs"]["state"] == "completed"

    def test_admission_denial_makes_zero_provider_calls(self, monkeypatch):
        recorded = _grant_hub(monkeypatch, granted=False, reason="refused")
        provider_calls = []

        def fake_run(argv, timeout=30.0, env=None, cwd=None, process_registry=None):
            provider_calls.append(argv)
            return _result(stdout="task_x")

        monkeypatch.setattr(codex_cloud.proc, "run", fake_run)
        result = codex_cloud.run_cloud_task(
            "x",
            env_id="e",
            timeout=600,
            sleep=lambda s: None,
            clock=lambda: 0,
        )
        assert not result.ok
        assert result.status == "denied"
        assert result.failure_kind == "fleet-admission-denied"
        assert provider_calls == []
        assert recorded["admit"]
        assert recorded["bind"] == []
        assert recorded["release"] == []

    def test_missing_lease_id_makes_zero_provider_calls(self, monkeypatch):
        recorded = _grant_hub(monkeypatch, lease_id=None)
        provider_calls = []

        def fake_run(argv, timeout=30.0, env=None, cwd=None, process_registry=None):
            provider_calls.append(argv)
            return _result(stdout="task_x")

        monkeypatch.setattr(codex_cloud.proc, "run", fake_run)
        result = codex_cloud.run_cloud_task(
            "x",
            env_id="e",
            timeout=600,
            sleep=lambda s: None,
            clock=lambda: 0,
        )
        assert not result.ok
        assert result.status == "denied"
        assert result.failure_kind == "fleet-admission-denied"
        assert provider_calls == []
        assert recorded["bind"] == []
        assert recorded["release"] == []

    def test_admission_label_uses_sanitized_provider_repo_hash(self, monkeypatch):
        recorded = _grant_hub(monkeypatch)
        fake = FakeRuns(
            {
                "exec": [_result(stdout="task_label1")],
                "status": [_result(stdout="Status: completed")],
                "diff": [_result(stdout="")],
            }
        )
        monkeypatch.setattr(codex_cloud.proc, "run", fake)
        prompt = "rotate the production credentials for acme corp"
        result = codex_cloud.run_cloud_task(
            prompt,
            env_id="e",
            timeout=600,
            poll_interval=0,
            sleep=lambda s: None,
            clock=lambda: 0,
        )
        assert result.ok
        admit_kwargs = recorded["admit"][0]["kwargs"]
        expected_digest = cloud_tracker.prompt_hash(prompt).removeprefix("sha256:")[:12]
        assert admit_kwargs["label"] == f"codex:owner/repo@{expected_digest}"
        assert len(admit_kwargs["label"]) <= cloud_tracker.LEASE_LABEL_MAX
        for word in prompt.split():
            assert word not in admit_kwargs["label"]
        assert prompt not in json.dumps(admit_kwargs)
        assert recorded["admit"][0]["args"][0] == "codex"
        assert admit_kwargs["repo"] == "owner/repo"

    @pytest.mark.parametrize(
        "exec_result",
        [
            _result(code=124, stderr="timeout after 5s"),
            _result(code=1, stderr="Error: no cloud environments"),
            _result(code=0, stdout="some prose without any identifier tokens"),
        ],
        ids=["timeout", "nonzero", "unparseable"],
    )
    def test_ambiguous_submit_holds_lease_and_does_not_release(self, monkeypatch, exec_result):
        recorded = _grant_hub(monkeypatch)
        registry_calls = []
        monkeypatch.setattr(
            cloud_tracker,
            "register",
            lambda *a, **k: registry_calls.append(k) or {"id": "registered"},
        )
        fake = FakeRuns({"exec": [exec_result]})
        monkeypatch.setattr(codex_cloud.proc, "run", fake)
        result = codex_cloud.run_cloud_task(
            "x",
            env_id="e",
            timeout=600,
            sleep=lambda s: None,
            clock=lambda: 0,
        )
        assert not result.ok
        assert result.status == "uncertain"
        assert result.failure_kind == "uncertain"
        assert recorded["bind"] == []
        assert recorded["release"] == []
        assert registry_calls == []

    def test_bind_failure_retains_lease(self, monkeypatch):
        recorded = _grant_hub(monkeypatch)
        monkeypatch.setattr(fleet_client, "bind_cloud", lambda *a, **k: fleet_client.CloudDecision(False, "hub-down"))
        fake = FakeRuns(
            {
                "exec": [_result(stdout="task_bindfail1")],
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
        assert result.status == "bind-failed"
        assert result.failure_kind == "fleet-bind-failed"
        assert result.thread_id == "task_bindfail1"
        assert recorded["release"] == []

    def test_terminal_failure_releases_lease(self, monkeypatch):
        recorded = _grant_hub(monkeypatch)
        fake = FakeRuns(
            {
                "exec": [_result(stdout="task_fail1")],
                "status": [_result(stdout="Status: failed")],
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
        assert recorded["release"]
        assert recorded["release"][0]["args"][0] == "lease-1"
        assert recorded["release"][0]["kwargs"]["state"] == "failed"

    def test_terminal_success_releases_lease_before_diff(self, monkeypatch):
        recorded = _grant_hub(monkeypatch)
        fake = FakeRuns(
            {
                "exec": [_result(stdout="task_okrelease1")],
                "status": [_result(stdout="Status: completed")],
                "diff": [_result(stdout="")],
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
        assert recorded["release"]
        assert recorded["release"][0]["kwargs"]["state"] == "completed"

    def test_nonterminal_status_polls_renew_lease(self, monkeypatch):
        recorded = _grant_hub(monkeypatch)
        fake = FakeRuns(
            {
                "exec": [_result(stdout="task_renew1")],
                "status": [
                    _result(stdout="Status: running"),
                    _result(stdout="Status: running"),
                    _result(stdout="Status: completed"),
                ],
                "diff": [_result(stdout="")],
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
        assert len(recorded["renew"]) == 2

    def test_diff_is_read_only_and_never_applies(self, monkeypatch):
        recorded = _grant_hub(monkeypatch)
        fake = FakeRuns(
            {
                "exec": [_result(stdout="task_noapply1")],
                "status": [_result(stdout="Status: completed")],
                "diff": [_result(stdout="diff --git a/f b/f\n+fixed")],
            }
        )
        monkeypatch.setattr(codex_cloud.proc, "run", fake)
        result = codex_cloud.run_cloud_task(
            "fix it",
            env_id="e",
            timeout=600,
            poll_interval=0,
            sleep=lambda s: None,
            clock=lambda: 0,
        )
        assert result.ok
        assert "NOT applied locally" in result.text
        assert not any(c[:3] == ["codex", "cloud", "apply"] for c in fake.calls)
        assert recorded["release"]

    def test_status_timeout_holds_lease_and_returns_uncertain(self, monkeypatch):
        recorded = _grant_hub(monkeypatch)
        fake = FakeRuns(
            {
                "exec": [_result(stdout="task_stimeout1")],
                "status": [_result(code=124, stderr="timeout after 5s")],
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
        assert result.status == "uncertain"
        assert result.failure_kind == "uncertain"
        assert result.timed_out is True
        assert recorded["release"] == []

    def test_unknown_status_neither_renews_nor_releases_on_poll_timeout(self, monkeypatch):
        recorded = _grant_hub(monkeypatch)
        fake = FakeRuns(
            {
                "exec": [_result(stdout="task_unknown1")],
                "status": [_result(stdout="Task: completed task\nprovider message without a status field")],
            }
        )
        monkeypatch.setattr(codex_cloud.proc, "run", fake)
        ticks = iter([0, 0, 601])

        result = codex_cloud.run_cloud_task(
            "x", env_id="e", timeout=600, poll_interval=0, sleep=lambda s: None, clock=lambda: next(ticks, 601)
        )

        assert result.status == "uncertain"
        assert recorded["renew"] == []
        assert recorded["release"] == []

    def test_config_error_fails_closed_before_provider_exec(self, monkeypatch):
        provider_calls = []
        monkeypatch.setattr(codex_cloud, "_hub_configured", lambda: (_ for _ in ()).throw(OSError("bad config")))
        monkeypatch.setattr(codex_cloud.proc, "run", lambda argv, **kwargs: provider_calls.append(argv) or _result())

        result = codex_cloud.run_cloud_task("x", env_id="e", timeout=600)

        assert result.status == "uncertain"
        assert result.failure_kind == "fleet-admission-unavailable"
        assert provider_calls == []

    def test_local_registry_uses_codex_cloud_hash_label_without_prompt(self, monkeypatch, tmp_path):
        registrations = []
        fake = FakeRuns(
            {
                "exec": [_result(stdout="task_registry1")],
                "status": [_result(stdout="Status: completed")],
                "diff": [_result(stdout="")],
            }
        )
        monkeypatch.setattr(codex_cloud.proc, "run", fake)
        monkeypatch.setattr(cloud_tracker, "register", lambda *args, **kwargs: registrations.append(kwargs))
        prompt = "private provider instructions"

        result = codex_cloud.run_cloud_task(
            prompt,
            env_id="e",
            timeout=600,
            register_target=tmp_path,
            label=prompt,
            poll_interval=0,
            sleep=lambda s: None,
            clock=lambda: 0,
        )

        assert result.ok
        assert registrations[0]["provider"] == "codex-cloud"
        assert registrations[0]["label"] == cloud_tracker.lease_label(
            "codex-cloud", None, cloud_tracker.prompt_hash(prompt)
        )
        assert prompt not in json.dumps(registrations[0])


class TestListTasks:
    def test_list_tasks_parses_dict_schema_and_sanitizes(self, monkeypatch):
        raw = {
            "tasks": [
                {
                    "id": "task_t01",
                    "status": "RUNNING",
                    "environment": {"id": "env-1", "name": "PRIVATE"},
                    "title": "PRIVATE",
                    "summary": "PRIVATE",
                    "prompt": "PRIVATE",
                },
                {"id": "task_t02", "status": "COMPLETED", "title": "PRIVATE"},
            ],
            "cursor": "c1",
        }
        monkeypatch.setattr(codex_cloud.proc, "run", lambda *a, **k: _result(stdout=json.dumps(raw)))
        tasks = codex_cloud.list_tasks()
        assert tasks == [
            {"id": "task_t01", "state": "running", "environment_id": "env-1"},
            {"id": "task_t02", "state": "completed"},
        ]
        assert "PRIVATE" not in json.dumps(tasks)
        assert "cursor" not in json.dumps(tasks)

    def test_list_tasks_rejects_undocumented_plain_array(self, monkeypatch):
        raw = [{"id": "task_t3", "state": "FAILED", "title": "PRIVATE"}]
        monkeypatch.setattr(codex_cloud.proc, "run", lambda *a, **k: _result(stdout=json.dumps(raw)))
        assert codex_cloud.list_tasks() == []

    def test_list_tasks_bounds_max_items(self, monkeypatch):
        raw = {"tasks": [{"id": f"task_t0{i}", "status": "RUNNING"} for i in range(5)]}
        monkeypatch.setattr(codex_cloud.proc, "run", lambda *a, **k: _result(stdout=json.dumps(raw)))
        tasks = codex_cloud.list_tasks(max_items=2)
        assert len(tasks) == 2
        assert tasks[0]["id"] == "task_t00"
        assert tasks[1]["id"] == "task_t01"

    def test_list_tasks_uses_bounded_cursor_pagination(self, monkeypatch):
        calls = []
        pages = [
            {"tasks": [{"id": "task_page1", "status": "RUNNING"}], "cursor": "next"},
            {"tasks": [{"id": "task_page2", "status": "COMPLETED"}]},
        ]

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return _result(stdout=json.dumps(pages.pop(0)))

        monkeypatch.setattr(codex_cloud.proc, "run", fake_run)
        assert codex_cloud.list_tasks(max_items=25) == [
            {"id": "task_page1", "state": "running"},
            {"id": "task_page2", "state": "completed"},
        ]
        assert calls[0][-2:] == ["--limit", "20"]
        assert calls[1][-4:] == ["--limit", "20", "--cursor", "next"]

    def test_list_tasks_returns_empty_on_command_failure(self, monkeypatch):
        monkeypatch.setattr(codex_cloud.proc, "run", lambda *a, **k: _result(code=1, stderr="boom"))
        assert codex_cloud.list_tasks() == []

    def test_list_tasks_returns_empty_on_non_json_output(self, monkeypatch):
        monkeypatch.setattr(codex_cloud.proc, "run", lambda *a, **k: _result(stdout="not json"))
        assert codex_cloud.list_tasks() == []


class TestEnvironmentConfig:
    def test_passthrough_literal_env_id(self, tmp_path):
        assert codex_cloud.resolve_environment_id("env-123", target=tmp_path) == "env-123"

    def test_env_var_selector_reads_named_variable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_CLOUD_ENV", "env-from-var")
        assert codex_cloud.resolve_environment_id("$CODEX_CLOUD_ENV", target=tmp_path) == "env-from-var"

    def test_env_var_selector_missing_variable_fails(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CODEX_CLOUD_ENV", raising=False)
        with pytest.raises(codex_cloud.CodexCloudConfigError, match="CODEX_CLOUD_ENV"):
            codex_cloud.resolve_environment_id("$CODEX_CLOUD_ENV", target=tmp_path)

    def test_configured_reads_env_var_reference(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_CLOUD_ENV", "env-configured")
        codex_cloud.save_environment_config(tmp_path, environment_id_env="CODEX_CLOUD_ENV")
        assert codex_cloud.resolve_environment_id("configured", target=tmp_path) == "env-configured"

    def test_configured_reads_inline_id_from_local_config(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CODEX_CLOUD_ENV", raising=False)
        codex_cloud.save_environment_config(tmp_path, environment_id="env-local-file")
        assert codex_cloud.resolve_environment_id("configured", target=tmp_path) == "env-local-file"

    def test_configured_falls_back_to_default_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_CLOUD_ENV", "env-default")
        assert codex_cloud.resolve_environment_id("configured", target=tmp_path) == "env-default"

    def test_configured_without_source_fails(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CODEX_CLOUD_ENV", raising=False)
        with pytest.raises(codex_cloud.CodexCloudConfigError, match="configured"):
            codex_cloud.resolve_environment_id("configured", target=tmp_path)

    def test_config_file_does_not_store_secret_material(self, tmp_path):
        path = codex_cloud.save_environment_config(tmp_path, environment_id_env="CODEX_CLOUD_ENV")
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["schema"] == "brigade.codex-cloud-config/1"
        assert payload["environment"] == {"kind": "env", "name": "CODEX_CLOUD_ENV"}
        assert "env-" not in path.read_text(encoding="utf-8")

    def test_run_agent_resolves_configured_before_dispatch(self, tmp_path, monkeypatch):
        seen: dict[str, str] = {}

        def fake_cloud_task(prompt, *, env_id, timeout, cwd=None, process_registry=None):
            seen["env_id"] = env_id
            return agents.AgentResult(text="ok", ok=True)

        monkeypatch.setenv("CODEX_CLOUD_ENV", "env-resolved")
        monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command)
        monkeypatch.setattr(codex_cloud, "run_cloud_task", fake_cloud_task)
        result = agents.run_agent("codex-cloud:configured", "fix it", cwd=tmp_path)
        assert result.ok
        assert seen["env_id"] == "env-resolved"

    def test_run_agent_unresolved_configured_does_not_dispatch(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.delenv("CODEX_CLOUD_ENV", raising=False)
        monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command)
        monkeypatch.setattr(
            codex_cloud, "run_cloud_task", lambda *a, **k: calls.append(k) or agents.AgentResult(ok=True)
        )
        result = agents.run_agent("codex-cloud:configured", "fix it", cwd=tmp_path)
        assert not result.ok
        assert result.failure_kind == "codex-cloud-env-unconfigured"
        assert calls == []

    def test_canary_lists_tasks_and_never_execs_or_applies(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_CLOUD_ENV", "env-canary")
        fake = FakeRuns(
            {
                "list": [_result(stdout=json.dumps({"tasks": [{"id": "task_c01", "status": "COMPLETED"}]}))],
                "exec": [_result(stdout="task_should_not_run")],
                "apply": [_result(stdout="applied")],
            }
        )
        monkeypatch.setattr(codex_cloud.proc, "run", fake)
        report = codex_cloud.canary(tmp_path)
        assert report["ok"] is True
        assert report["environment_configured"] is True
        assert report["task_count"] == 1
        assert report["apply"] is False
        assert "env-canary" not in json.dumps(report)
        assert all(call[2] == "list" for call in fake.calls)


def test_codex_cloud_configured_ref_is_known():
    assert agents.is_known("codex-cloud:configured")
    assert agents.is_known("codex-cloud:$CODEX_CLOUD_ENV")
    assert agents.command_for("codex-cloud:configured") == "codex"


class TestCodexCloudCli:
    def test_setup_writes_env_var_reference(self, tmp_path, capsys):
        rc = cli.main(
            [
                "run",
                "cloud",
                "setup",
                "--provider",
                "codex-cloud",
                "--target",
                str(tmp_path),
                "--env-var",
                "CODEX_CLOUD_ENV",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "codex-cloud config saved" in out
        payload = json.loads((tmp_path / ".brigade" / "cloud" / "codex.json").read_text(encoding="utf-8"))
        assert payload["environment"] == {"kind": "env", "name": "CODEX_CLOUD_ENV"}

    def test_setup_rejects_missing_source(self, tmp_path):
        with pytest.raises(SystemExit) as exc:
            cli.main(["run", "cloud", "setup", "--provider", "codex-cloud", "--target", str(tmp_path)])
        assert exc.value.code == 2

    def test_doctor_reports_unconfigured_without_env_id(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv("CODEX_CLOUD_ENV", raising=False)
        rc = cli.main(["run", "cloud", "doctor", "--provider", "codex-cloud", "--target", str(tmp_path), "--json"])
        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert payload["environment_configured"] is False
        assert "env-" not in json.dumps(payload)

    def test_canary_cli_never_submits(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("CODEX_CLOUD_ENV", "env-canary")
        calls = []

        def fake_run(argv, timeout=30.0, env=None, cwd=None, process_registry=None):
            calls.append(argv)
            return _result(stdout=json.dumps({"tasks": []}))

        monkeypatch.setattr(codex_cloud.proc, "run", fake_run)
        rc = cli.main(["run", "cloud", "canary", "--provider", "codex-cloud", "--target", str(tmp_path), "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["apply"] is False
        assert "env-canary" not in json.dumps(payload)
        assert all(argv[2] == "list" for argv in calls)
        assert not any(argv[2] in {"exec", "apply"} for argv in calls)
