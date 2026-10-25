import hashlib
import json
import subprocess
import sys

import pytest

from brigade import (
    agents,
    cli,
    cloud_tracker,
    codex_cloud,
    cursor_cloud,
    fleet_client,
    fleet_client_grokbot,
    jules_cloud,
    proc,
)

# This module drives the real codex_cloud.list_tasks through its own fake proc.run,
# so it opts out of the suite-wide inventory stub (#1475).
pytestmark = pytest.mark.real_codex_cloud


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
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c + ".exe")
    result = agents.run_agent("codex-cloud:", "hi")
    assert not result.ok
    assert "environment id" in result.detail


def test_run_agent_rejects_model_pin(monkeypatch):
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c + ".exe")
    result = agents.run_agent("codex-cloud:env-123", "hi", model="gpt-5.5")
    assert not result.ok
    assert "model" in result.detail


def test_run_agent_threads_process_registry_to_codex_cloud(monkeypatch):
    registry = proc.ProcessRegistry()
    seen = {}

    def fake_cloud_task(prompt, *, env_id, timeout, cwd=None, process_registry=None, **kwargs):
        seen.update(
            prompt=prompt,
            env_id=env_id,
            timeout=timeout,
            cwd=cwd,
            process_registry=process_registry,
        )
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command + ".exe")
    monkeypatch.setattr(codex_cloud, "run_cloud_task", fake_cloud_task)

    result = agents.run_agent("codex-cloud:env-123", "fix it", process_registry=registry)

    assert result.ok
    assert seen["process_registry"] is registry


def test_run_agent_preserves_legacy_codex_cloud_call_shape(monkeypatch):
    def fake_cloud_task(prompt, *, env_id, timeout, cwd=None, process_registry=None):
        del process_registry
        return agents.AgentResult(text=f"{env_id}: {prompt}", ok=True)

    monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command + ".exe")
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


def _assert_windows_hub_refusal(monkeypatch, target, *, operation="launch", capsys=None):
    """Assert the approved platform contract instead of exercising unavailable storage."""
    calls = _grant_hub(monkeypatch)

    def forbidden(*args, **kwargs):
        pytest.fail("unsupported Windows Hub operation reached I/O")

    monkeypatch.setattr(codex_cloud.proc, "run", forbidden)
    monkeypatch.setattr(codex_cloud, "_preflight_registry", forbidden)
    monkeypatch.setattr(codex_cloud, "_lease_recovery_path", forbidden)
    monkeypatch.setattr(cloud_tracker, "load_registry", forbidden)
    monkeypatch.setattr(cloud_tracker, "register", forbidden)
    monkeypatch.setattr(fleet_client, "fetch_cloud", forbidden)
    if operation.startswith("cli-"):
        monkeypatch.setenv("BRIGADE_EXTRAS", "1")
        command = operation.removeprefix("cli-")
        argv = ["run", "cloud", command, "--target", str(target), "--provider", "codex-cloud", "--label", "test"]
        argv += (
            ["--repo", "owner/repo", "--prompt-file", str(target / "missing")]
            if command == "launch"
            else ["--task-id", "task_windows99"]
        )
        assert cli.main(argv + ["--json"]) == 2
        captured = capsys.readouterr()
        assert "secure-owner-store-unavailable" in captured.out + captured.err
    elif operation == "register":
        with pytest.raises(OSError, match="^secure-owner-store-unavailable$"):
            codex_cloud.acquire_registered_task_lease(
                target, task_id="task_windows99", label="test", prompt_hash=None, repo="owner/repo"
            )
    elif operation == "roster":
        result = codex_cloud.run_cloud_task("private prompt", env_id="env-local", timeout=1, register_target=target)
        assert not result.ok
        assert result.status == "denied"
        assert result.failure_phase == "dispatch"
        assert result.failure_kind == result.detail == "secure-owner-store-unavailable"
        assert result.thread_id is None
    elif operation == "storage":
        with pytest.raises(OSError, match="^secure-owner-store-unavailable$"):
            codex_cloud._persist_lease_holder(target, "lease-1", "private-holder")
        assert codex_cloud._recover_lease_holder(target, "lease-1") is None
    else:
        assert codex_cloud.launch_task("private prompt", target=target, repo="owner/repo", label="test") == {
            "ok": False,
            "reason": "secure-owner-store-unavailable",
        }
    assert all(not values for values in calls.values())
    assert not (target / ".brigade").exists()


def test_launch_task_uses_stdin_and_binds_codex_lease(monkeypatch, tmp_path):
    if sys.platform == "win32":
        _assert_windows_hub_refusal(monkeypatch, tmp_path)
        return
    codex_cloud.save_environment_config(tmp_path, environment_id="env-launch")
    calls = _grant_hub(monkeypatch)
    seen = {}

    def fake_run(argv, timeout=30.0, env=None, cwd=None, stdin=None, process_registry=None):
        seen.update(argv=argv, stdin=stdin, cwd=cwd)
        return _result(stdout="Submitted https://chatgpt.com/codex/tasks/task_launch99")

    monkeypatch.setattr(codex_cloud.proc, "run", fake_run)
    launched = codex_cloud.launch_task("private prompt", target=tmp_path, repo="owner/repo", label="launch")

    assert launched["ok"] is True
    assert launched["task_id"] == "task_launch99"
    assert seen["argv"] == ["codex", "cloud", "exec", "--env", "env-launch", "-"]
    assert seen["stdin"] == b"private prompt"
    assert "private prompt" not in seen["argv"]
    assert calls["admit"][0]["args"][0] == "codex"
    assert calls["bind"][0]["kwargs"]["provider_task_id"] == "task_launch99"
    assert calls["bind"][0]["kwargs"]["ttl_seconds"] == fleet_client.CLOUD_BOUND_TTL_SECONDS
    assert calls["bind"][0]["kwargs"]["holder"] == "h1"
    assert cloud_tracker.load_registry(tmp_path)["entries"][0]["task_id"] == "task_launch99"


def test_launch_task_releases_lease_when_submission_fails(monkeypatch, tmp_path):
    if sys.platform == "win32":
        _assert_windows_hub_refusal(monkeypatch, tmp_path)
        return
    codex_cloud.save_environment_config(tmp_path, environment_id="env-launch")
    calls = _grant_hub(monkeypatch)
    monkeypatch.setattr(codex_cloud.proc, "run", lambda *a, **k: _result(code=1))

    launched = codex_cloud.launch_task("private prompt", target=tmp_path, repo="owner/repo", label="launch")

    assert launched == {"ok": False, "reason": "submit-failed"}
    assert calls["release"][0]["kwargs"]["state"] == "submit-failed"


def test_launch_task_keeps_lease_when_submission_times_out(monkeypatch, tmp_path):
    if sys.platform == "win32":
        _assert_windows_hub_refusal(monkeypatch, tmp_path)
        return
    codex_cloud.save_environment_config(tmp_path, environment_id="env-launch")
    calls = _grant_hub(monkeypatch)
    monkeypatch.setattr(codex_cloud.proc, "run", lambda *a, **k: _result(code=124))

    launched = codex_cloud.launch_task("private prompt", target=tmp_path, repo="owner/repo", label="launch")

    assert launched == {"ok": False, "reason": "uncertain"}
    assert calls["release"] == []


def test_register_codex_task_lease_is_idempotent(monkeypatch, tmp_path):
    if sys.platform == "win32":
        _assert_windows_hub_refusal(monkeypatch, tmp_path, operation="register")
        return
    calls = _grant_hub(monkeypatch)
    monkeypatch.setattr(fleet_client, "fetch_cloud", lambda: {"leases": []})
    cloud_tracker.register(
        tmp_path,
        provider="codex-cloud",
        task_id="task_register99",
        label="registered task",
        prompt_hash="sha256:1234",
    )

    codex_cloud.acquire_registered_task_lease(
        tmp_path,
        task_id="task_register99",
        label="registered task",
        prompt_hash="sha256:1234",
        repo="owner/repo",
    )
    codex_cloud.acquire_registered_task_lease(
        tmp_path,
        task_id="task_register99",
        label="registered task",
        prompt_hash="sha256:1234",
        repo="owner/repo",
    )

    assert len(calls["admit"]) == 1
    assert len(calls["bind"]) == 1
    assert calls["bind"][0]["kwargs"]["ttl_seconds"] == fleet_client.CLOUD_BOUND_TTL_SECONDS
    assert calls["bind"][0]["kwargs"]["holder"] == "h1"
    assert cloud_tracker.load_registry(tmp_path)["entries"][0]["lease_holder"] == "h1"


def test_cli_cloud_launch_codex_cloud_submission(monkeypatch, tmp_path, capsys):
    if sys.platform == "win32":
        _assert_windows_hub_refusal(monkeypatch, tmp_path, operation="cli-launch", capsys=capsys)
        return
    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    codex_cloud.save_environment_config(tmp_path, environment_id="env-cli")
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("private prompt", encoding="utf-8")
    prompt_file.chmod(0o600)
    calls = _grant_hub(monkeypatch)
    seen = {}

    def fake_run(argv, timeout=30.0, env=None, cwd=None, stdin=None, process_registry=None):
        seen.update(argv=argv, stdin=stdin)
        return _result(stdout="task_cli_launch99")

    monkeypatch.setattr(codex_cloud.proc, "run", fake_run)
    rc = cli.main(
        [
            "run",
            "cloud",
            "launch",
            "--target",
            str(tmp_path),
            "--provider",
            "codex-cloud",
            "--repo",
            "owner/repo",
            "--label",
            "cli-codex",
            "--prompt-file",
            str(prompt_file),
            "--json",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["task_id"] == "task_cli_launch99"
    assert payload["provider"] == "codex-cloud"
    assert "holder" not in payload
    assert seen["argv"] == ["codex", "cloud", "exec", "--env", "env-cli", "-"]
    assert seen["stdin"] == b"private prompt"
    assert len(calls["admit"]) == len(calls["bind"]) == 1


def test_cli_register_codex_cloud_acquires_one_lease(monkeypatch, tmp_path, capsys):
    if sys.platform == "win32":
        _assert_windows_hub_refusal(monkeypatch, tmp_path, operation="cli-register", capsys=capsys)
        return
    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    calls = _grant_hub(monkeypatch)
    monkeypatch.setattr(fleet_client, "fetch_cloud", lambda: {"leases": []})
    argv = [
        "run",
        "cloud",
        "register",
        "--target",
        str(tmp_path),
        "--provider",
        "codex-cloud",
        "--task-id",
        "task_cli_register99",
        "--label",
        "registered",
        "--json",
    ]

    assert cli.main(argv) == 0
    first = json.loads(capsys.readouterr().out)
    assert cli.main(argv) == 0
    second_output = capsys.readouterr()
    second = json.loads(second_output.out)
    assert "lease_holder" not in first
    assert "lease_holder" not in second
    assert "h1" not in second_output.out + second_output.err

    assert first["task_id"] == "task_cli_register99"
    assert len(calls["admit"]) == 1
    assert len(calls["bind"]) == 1


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("code", [1, 124])
@pytest.mark.parametrize("hub_configured", [True, False])
def test_launch_task_retains_known_work_on_nonzero_exit(monkeypatch, tmp_path, stream, code, hub_configured):
    if sys.platform == "win32" and hub_configured:
        _assert_windows_hub_refusal(monkeypatch, tmp_path)
        return
    codex_cloud.save_environment_config(tmp_path, environment_id="env-launch")
    calls = _grant_hub(monkeypatch)
    monkeypatch.setattr(codex_cloud, "_hub_configured", lambda: hub_configured)
    submits = []

    def fake_run(argv, **kwargs):
        if argv[:3] != ["codex", "cloud", "exec"]:
            return _result(code=1)
        submits.append((argv, kwargs))
        return _result(code=code, **{stream: "Submitted task_recover99"})

    monkeypatch.setattr(codex_cloud.proc, "run", fake_run)

    launched = codex_cloud.launch_task("private prompt", target=tmp_path, repo="owner/repo", label="launch")

    assert launched["task_id"] == "task_recover99"
    assert launched["ok"] is False
    assert launched["reason"] == "uncertain"
    assert len(submits) == 1
    assert calls["release"] == []
    assert len(calls["bind"]) == int(hub_configured)
    if hub_configured:
        assert calls["bind"][0]["kwargs"]["provider_task_id"] == "task_recover99"
        assert calls["bind"][0]["kwargs"]["holder"] == "h1"
        assert calls["bind"][0]["kwargs"]["ttl_seconds"] == fleet_client.CLOUD_BOUND_TTL_SECONDS
    entry = cloud_tracker.load_registry(tmp_path)["entries"][0]
    assert entry["task_id"] == "task_recover99"
    if hub_configured:
        assert entry["lease_holder"] == "h1"
    else:
        assert "lease_holder" not in entry


@pytest.mark.parametrize("failure", ["open", "write", "fsync"])
def test_launch_task_registry_preflight_fails_before_admission(monkeypatch, tmp_path, failure):
    if sys.platform == "win32":
        _assert_windows_hub_refusal(monkeypatch, tmp_path)
        return
    codex_cloud.save_environment_config(tmp_path, environment_id="env-launch")
    calls = _grant_hub(monkeypatch)
    monkeypatch.setattr(codex_cloud.proc, "run", lambda *a, **k: pytest.fail("provider invoked"))

    def fail(*args, **kwargs):
        raise OSError("registry durability unavailable")

    monkeypatch.setattr(codex_cloud.os, failure, fail)

    launched = codex_cloud.launch_task("private prompt", target=tmp_path, repo="owner/repo", label="launch")

    assert launched == {"ok": False, "reason": "registry-unwritable"}
    assert all(not values for values in calls.values())
    assert not list(cloud_tracker.registry_path(tmp_path).parent.glob(".registry-preflight-*"))


@pytest.mark.parametrize("live_bytes", [b'{"entries": []}\n', b"invalid existing registry\n"])
def test_codex_registry_preflight_preserves_live_registry(monkeypatch, tmp_path, live_bytes):
    registry = cloud_tracker.registry_path(tmp_path)
    registry.parent.mkdir(parents=True)
    registry.write_bytes(live_bytes)
    monkeypatch.setattr(cloud_tracker, "load_registry", lambda *a, **k: pytest.fail("live registry read"))
    monkeypatch.setattr(cloud_tracker, "save_registry", lambda *a, **k: pytest.fail("live registry write"))

    assert codex_cloud._preflight_registry(tmp_path)

    assert registry.read_bytes() == live_bytes
    assert not list(registry.parent.glob(".registry-preflight-*"))


@pytest.mark.parametrize("hub_configured", [True, False])
def test_launch_task_tracking_failure_preserves_task_and_lease(monkeypatch, tmp_path, hub_configured):
    if sys.platform == "win32" and hub_configured:
        _assert_windows_hub_refusal(monkeypatch, tmp_path)
        return
    codex_cloud.save_environment_config(tmp_path, environment_id="env-launch")
    calls = _grant_hub(monkeypatch)
    monkeypatch.setattr(codex_cloud, "_hub_configured", lambda: hub_configured)
    monkeypatch.setattr(codex_cloud.proc, "run", lambda *a, **k: _result(stdout="task_tracking99"))

    def fail_register(*args, **kwargs):
        raise OSError("disk changed after preflight")

    monkeypatch.setattr(cloud_tracker, "register", fail_register)

    launched = codex_cloud.launch_task("private prompt", target=tmp_path, repo="owner/repo", label="launch")

    assert launched == {"ok": False, "reason": "tracking-failed", "task_id": "task_tracking99"}
    assert len(calls["bind"]) == int(hub_configured)
    assert calls["release"] == []


@pytest.mark.parametrize("code", [0, 1])
def test_launch_task_bind_failure_preserves_known_work(monkeypatch, tmp_path, code):
    if sys.platform == "win32":
        _assert_windows_hub_refusal(monkeypatch, tmp_path)
        return
    codex_cloud.save_environment_config(tmp_path, environment_id="env-launch")
    calls = _grant_hub(monkeypatch)
    monkeypatch.setattr(codex_cloud.proc, "run", lambda *a, **k: _result(code=code, stdout="task_bind99"))
    monkeypatch.setattr(
        fleet_client, "bind_cloud", lambda *a, **k: fleet_client.CloudDecision(granted=False, reason="unavailable")
    )

    launched = codex_cloud.launch_task("private prompt", target=tmp_path, repo="owner/repo", label="launch")

    assert launched == {"ok": False, "reason": "bind-failed", "task_id": "task_bind99"}
    assert calls["release"] == []
    assert cloud_tracker.load_registry(tmp_path)["entries"] == []


@pytest.mark.parametrize(
    "flag", [["--auto-create-pr"], ["--starting-branch", "main"], ["--title", "title"], ["--no-plan-approval"]]
)
def test_cli_codex_launch_rejects_unsupported_flags_before_mutation(monkeypatch, tmp_path, capsys, flag):
    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    codex_cloud.save_environment_config(tmp_path, environment_id="env-launch")
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("private prompt", encoding="utf-8")
    prompt_file.chmod(0o600)
    calls = _grant_hub(monkeypatch)
    provider_calls = []

    def fake_run(*args, **kwargs):
        provider_calls.append((args, kwargs))
        return _result(stdout="task_unsupported99")

    monkeypatch.setattr(codex_cloud.proc, "run", fake_run)
    rc = cli.main(
        [
            "run",
            "cloud",
            "launch",
            "--target",
            str(tmp_path),
            "--provider",
            "codex-cloud",
            "--repo",
            "owner/repo",
            "--label",
            "launch",
            "--prompt-file",
            str(prompt_file),
            "--json",
            *flag,
        ]
    )

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": False, "provider": "codex-cloud", "label": "launch", "reason": "unsupported-flag"}
    assert all(not values for values in calls.values())
    assert provider_calls == []
    assert cloud_tracker.load_registry(tmp_path)["entries"] == []


@pytest.mark.parametrize("reason", ["tracking-failed", "bind-failed", "uncertain"])
def test_cli_codex_failure_text_preserves_only_sanitized_task_id(monkeypatch, tmp_path, capsys, reason):
    from brigade.cli import run_cloud

    # This test covers public error formatting; prompt-file security has separate coverage.
    monkeypatch.setattr(codex_cloud, "_hub_configured", lambda: False)
    monkeypatch.setattr(run_cloud, "_read_prompt_file", lambda path: "private prompt")
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("private prompt", encoding="utf-8")
    prompt_file.chmod(0o600)
    monkeypatch.setattr(
        codex_cloud,
        "launch_task",
        lambda *a, **k: {"ok": False, "reason": reason, "task_id": "task_recover99", "holder": "private-holder"},
    )
    rc = cli.main(
        [
            "run",
            "cloud",
            "launch",
            "--target",
            str(tmp_path),
            "--provider",
            "codex-cloud",
            "--repo",
            "owner/repo",
            "--label",
            "launch",
            "--prompt-file",
            str(prompt_file),
        ]
    )
    output = capsys.readouterr()
    assert rc == 1
    assert output.err == f"error: {reason} task=task_recover99\n"
    assert "private-holder" not in output.out + output.err
    assert "private prompt" not in output.out + output.err


@pytest.mark.parametrize("task_id", ["task_valid99\nholder=private-holder", "invalid", "task_valid99\x1b[31m"])
def test_cli_codex_failure_text_rejects_unsanitized_task_id(task_id, capsys):
    from brigade.cli import run_cloud

    run_cloud._emit_launch_payload(
        {"ok": False, "provider": "codex-cloud", "reason": "uncertain", "task_id": task_id},
        as_json=False,
        exit_code=1,
    )
    assert capsys.readouterr().err == "error: uncertain\n"


@pytest.mark.parametrize("state", ["running", "completed"])
def test_codex_tracking_failure_recovers_holder_for_manual_registration_and_sync(monkeypatch, tmp_path, state):
    if sys.platform == "win32":
        _assert_windows_hub_refusal(monkeypatch, tmp_path)
        return
    codex_cloud.save_environment_config(tmp_path, environment_id="env-launch")
    calls = _grant_hub(monkeypatch)
    monkeypatch.setattr(codex_cloud.proc, "run", lambda *a, **k: _result(stdout="task_recovery99"))
    with monkeypatch.context() as failed_tracking:

        def fail(*args, **kwargs):
            raise OSError("registry changed after submission")

        failed_tracking.setattr(cloud_tracker, "register", fail)
        launched = codex_cloud.launch_task("private prompt", target=tmp_path, repo="owner/repo", label="launch")
    assert launched == {"ok": False, "reason": "tracking-failed", "task_id": "task_recovery99"}
    assert calls["release"] == []
    leases = [{"lease_id": "lease-1", "provider": "codex", "provider_task_id": "task_recovery99", "state": state}]
    monkeypatch.setattr(fleet_client, "fetch_cloud", lambda: {"leases": leases})
    cloud_tracker.register(tmp_path, provider="codex-cloud", task_id="task_recovery99", label="launch")
    codex_cloud.acquire_registered_task_lease(
        tmp_path,
        task_id="task_recovery99",
        label="launch",
        prompt_hash=None,
        repo="owner/repo",
    )
    assert cloud_tracker.load_registry(tmp_path)["entries"][0]["lease_holder"] == "h1"
    assert len(calls["admit"]) == len(calls["bind"]) == 1
    payload = cloud_tracker.sync_payload(
        tmp_path,
        provider_tasks={"task_recovery99": {"state": state}},
        github={"branches": [], "prs": []},
        hub_leases=leases,
    )
    operation = "renew" if state == "running" else "release"
    assert calls[operation][0]["args"][0] == "lease-1"
    assert calls[operation][0]["kwargs"]["holder"] == "h1"
    assert "h1" not in json.dumps(payload)


def test_codex_holder_recovery_is_durable_before_provider_submission(monkeypatch, tmp_path):
    if sys.platform == "win32":
        _assert_windows_hub_refusal(monkeypatch, tmp_path)
        return
    codex_cloud.save_environment_config(tmp_path, environment_id="env-launch")
    _grant_hub(monkeypatch)

    def fake_run(argv, **kwargs):
        if argv[:3] != ["codex", "cloud", "exec"]:
            return _result(code=1)
        records = list((cloud_tracker.registry_path(tmp_path).parent / "codex-leases").glob("*.json"))
        assert len(records) == 1
        assert json.loads(records[0].read_text())["holder"] == "h1"
        if codex_cloud.os.name != "nt":
            assert records[0].stat().st_mode & 0o777 == 0o600
        return _result(stdout="task_durable99")

    monkeypatch.setattr(codex_cloud.proc, "run", fake_run)
    assert codex_cloud.launch_task("private prompt", target=tmp_path, repo="owner/repo", label="launch")["ok"]


def test_codex_holder_recovery_write_failure_prevents_provider_submission(monkeypatch, tmp_path):
    if sys.platform == "win32":
        _assert_windows_hub_refusal(monkeypatch, tmp_path)
        return
    codex_cloud.save_environment_config(tmp_path, environment_id="env-launch")
    calls = _grant_hub(monkeypatch)

    def fake_run(argv, **kwargs):
        if argv[:3] == ["codex", "cloud", "exec"]:
            pytest.fail("provider invoked")
        return _result(code=1)

    monkeypatch.setattr(codex_cloud.proc, "run", fake_run)

    def fail(*args, **kwargs):
        raise OSError("private recovery record unavailable")

    monkeypatch.setattr(codex_cloud, "_persist_lease_holder", fail)
    launched = codex_cloud.launch_task("private prompt", target=tmp_path, repo="owner/repo", label="launch")
    assert launched == {"ok": False, "reason": "lease-recovery-unwritable"}
    assert calls["bind"] == []
    assert calls["release"][0]["kwargs"] == {"state": "submit-failed", "holder": "h1"}


def test_codex_existing_lease_without_private_recovery_fails_closed(monkeypatch, tmp_path):
    if sys.platform == "win32":
        _assert_windows_hub_refusal(monkeypatch, tmp_path, operation="register")
        return
    calls = _grant_hub(monkeypatch)
    cloud_tracker.register(tmp_path, provider="codex-cloud", task_id="task_legacy99", label="legacy")
    monkeypatch.setattr(
        fleet_client,
        "fetch_cloud",
        lambda: {
            "leases": [{"lease_id": "legacy-lease", "provider_task_id": "task_legacy99"}],
        },
    )
    with pytest.raises(RuntimeError, match="cloud lease holder unavailable"):
        codex_cloud.acquire_registered_task_lease(
            tmp_path,
            task_id="task_legacy99",
            label="legacy",
            prompt_hash=None,
            repo="owner/repo",
        )
    assert all(not values for values in calls.values())


def test_codex_registry_preflight_unlinks_sidecar_after_close_failure(monkeypatch, tmp_path):
    original_close = codex_cloud.os.close

    def close_then_fail(descriptor):
        original_close(descriptor)
        raise OSError("close failed")

    monkeypatch.setattr(codex_cloud.os, "close", close_then_fail)
    assert not codex_cloud._preflight_registry(tmp_path)
    assert not list(cloud_tracker.registry_path(tmp_path).parent.glob(".registry-preflight-*"))


def _recovery_record_bytes(holder="h1"):
    return json.dumps({"schema": "brigade.codex-lease-recovery/1", "lease_id": "lease-1", "holder": holder}).encode()


@pytest.mark.parametrize(
    "kind",
    [
        "symlink",
        pytest.param("public", marks=pytest.mark.skipif(codex_cloud.os.name != "posix", reason="POSIX mode bits")),
        "hardlink",
        "directory",
    ],
)
def test_recovery_v3_rejects_unsafe_record_reads(tmp_path, kind):
    path = codex_cloud._lease_recovery_path(tmp_path, "lease-1")
    path.parent.mkdir(mode=0o700, parents=True)
    victim = tmp_path / "victim.json"
    victim.write_bytes(_recovery_record_bytes())
    victim.chmod(0o600)
    if kind == "symlink":
        victim.chmod(0o666)
        path.symlink_to(victim)
    elif kind == "hardlink":
        path.hardlink_to(victim)
    elif kind == "directory":
        path.mkdir(mode=0o700)
    else:
        path.write_bytes(_recovery_record_bytes())
        path.chmod(0o666)
    before = path.lstat()

    assert codex_cloud._recover_lease_holder(tmp_path, "lease-1") is None

    after = path.lstat()
    assert (before.st_mode, before.st_ino) == (after.st_mode, after.st_ino)
    assert victim.read_bytes() == _recovery_record_bytes()


@pytest.mark.parametrize(
    "kind",
    [
        "symlink",
        pytest.param("public", marks=pytest.mark.skipif(codex_cloud.os.name != "posix", reason="POSIX mode bits")),
        "hardlink",
        "directory",
        "fifo",
    ],
)
def test_recovery_v3_rejects_unsafe_existing_record_writes(tmp_path, kind):
    if kind == "fifo" and not hasattr(codex_cloud.os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    path = codex_cloud._lease_recovery_path(tmp_path, "lease-1")
    path.parent.mkdir(mode=0o700, parents=True)
    victim = tmp_path / "victim.json"
    victim.write_bytes(_recovery_record_bytes())
    victim.chmod(0o600)
    if kind == "symlink":
        path.symlink_to(victim)
    elif kind == "hardlink":
        path.hardlink_to(victim)
    elif kind == "directory":
        path.mkdir(mode=0o700)
    elif kind == "fifo":
        codex_cloud.os.mkfifo(path, 0o600)
    else:
        path.write_bytes(_recovery_record_bytes())
        path.chmod(0o666)
    before = path.lstat()

    with pytest.raises(OSError):
        codex_cloud._persist_lease_holder(tmp_path, "lease-1", "h1")

    after = path.lstat()
    assert (before.st_mode, before.st_ino) == (after.st_mode, after.st_ino)
    assert victim.read_bytes() == _recovery_record_bytes()


def test_recovery_v3_refuses_symlinked_directory_for_reads_and_writes(tmp_path):
    path = codex_cloud._lease_recovery_path(tmp_path, "lease-1")
    path.parent.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    victim = outside / path.name
    victim.write_bytes(_recovery_record_bytes())
    victim.chmod(0o600)
    path.parent.symlink_to(outside, target_is_directory=True)
    before = victim.read_bytes()

    assert codex_cloud._recover_lease_holder(tmp_path, "lease-1") is None
    with pytest.raises(OSError):
        codex_cloud._persist_lease_holder(tmp_path, "lease-1", "h1")

    assert victim.read_bytes() == before
    assert list(outside.iterdir()) == [victim]


@pytest.mark.skipif(codex_cloud.os.name != "posix", reason="POSIX ownership and mode checks")
def test_recovery_v3_refuses_nonprivate_directory_without_chmod(tmp_path):
    path = codex_cloud._lease_recovery_path(tmp_path, "lease-1")
    path.parent.mkdir(parents=True)
    path.parent.chmod(0o755)
    path.write_bytes(_recovery_record_bytes())
    path.chmod(0o600)

    assert codex_cloud._recover_lease_holder(tmp_path, "lease-1") is None
    with pytest.raises(OSError):
        codex_cloud._persist_lease_holder(tmp_path, "lease-1", "h1")
    assert path.parent.stat().st_mode & 0o777 == 0o755


@pytest.mark.skipif(codex_cloud.os.name != "posix", reason="POSIX UID validation")
def test_recovery_v3_refuses_foreign_record_descriptor(monkeypatch, tmp_path):
    path = codex_cloud._lease_recovery_path(tmp_path, "lease-1")
    path.parent.mkdir(mode=0o700, parents=True)
    path.write_bytes(_recovery_record_bytes())
    path.chmod(0o600)
    identity = (path.stat().st_dev, path.stat().st_ino)
    real_fstat = codex_cloud.os.fstat

    def foreign_owner(descriptor):
        info = real_fstat(descriptor)
        if (info.st_dev, info.st_ino) == identity:
            fields = list(info)
            fields[4] += 1
            return codex_cloud.os.stat_result(fields)
        return info

    monkeypatch.setattr(codex_cloud.os, "fstat", foreign_owner)
    assert codex_cloud._recover_lease_holder(tmp_path, "lease-1") is None


def test_recovery_v3_bounds_record_bytes(tmp_path):
    path = codex_cloud._lease_recovery_path(tmp_path, "lease-1")
    path.parent.mkdir(mode=0o700, parents=True)
    path.write_bytes(_recovery_record_bytes("x" * 5000))
    path.chmod(0o600)
    assert codex_cloud._recover_lease_holder(tmp_path, "lease-1") is None


@pytest.mark.skipif(not hasattr(codex_cloud.os, "mkfifo"), reason="FIFO creation is unavailable")
def test_recovery_v3_fifo_read_is_bounded(tmp_path):
    path = codex_cloud._lease_recovery_path(tmp_path, "lease-1")
    path.parent.mkdir(mode=0o700, parents=True)
    codex_cloud.os.mkfifo(path, 0o600)
    script = (
        "import sys; from pathlib import Path; from brigade import codex_cloud; "
        "assert codex_cloud._recover_lease_holder(Path(sys.argv[1]), 'lease-1') is None"
    )
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, timeout=3)
    assert result.returncode == 0, result.stderr.decode()


def test_recovery_v3_fsyncs_new_directory_parent_before_record(monkeypatch, tmp_path):
    if sys.platform == "win32":
        _assert_windows_hub_refusal(monkeypatch, tmp_path, operation="storage")
        return
    path = codex_cloud._lease_recovery_path(tmp_path, "lease-1")
    path.parent.parent.mkdir(parents=True)
    parent = path.parent.parent.stat()
    synced = []
    real_fsync = codex_cloud.os.fsync

    def record_sync(descriptor):
        info = codex_cloud.os.fstat(descriptor)
        synced.append((info.st_dev, info.st_ino))
        return real_fsync(descriptor)

    monkeypatch.setattr(codex_cloud.os, "fsync", record_sync)
    codex_cloud._persist_lease_holder(tmp_path, "lease-1", "h1")
    record, directory = path.stat(), path.parent.stat()
    parent_key = (parent.st_dev, parent.st_ino)
    record_key = (record.st_dev, record.st_ino)
    directory_key = (directory.st_dev, directory.st_ino)
    assert parent_key in synced
    assert synced.index(parent_key) < synced.index(record_key) < synced.index(directory_key)


def test_recovery_v3_parent_fsync_failure_prevents_submission(monkeypatch, tmp_path):
    if sys.platform == "win32":
        _assert_windows_hub_refusal(monkeypatch, tmp_path)
        return
    codex_cloud.save_environment_config(tmp_path, environment_id="env-launch")
    calls = _grant_hub(monkeypatch)
    parent = cloud_tracker.registry_path(tmp_path).parent.stat()
    provider_calls = []
    real_fsync = codex_cloud.os.fsync

    def fail_parent_sync(descriptor):
        info = codex_cloud.os.fstat(descriptor)
        if (info.st_dev, info.st_ino) == (parent.st_dev, parent.st_ino):
            raise OSError("parent directory fsync failed")
        return real_fsync(descriptor)

    def fake_run(argv, **kwargs):
        if argv[:3] == ["codex", "cloud", "exec"]:
            provider_calls.append(argv)
            return _result(stdout="task_parent99")
        return _result(code=1)

    monkeypatch.setattr(codex_cloud.os, "fsync", fail_parent_sync)
    monkeypatch.setattr(codex_cloud.proc, "run", fake_run)
    launched = codex_cloud.launch_task("private prompt", target=tmp_path, repo="owner/repo", label="launch")
    assert launched == {"ok": False, "reason": "lease-recovery-unwritable"}
    assert provider_calls == []
    assert calls["bind"] == []
    assert calls["release"][0]["kwargs"]["holder"] == "h1"


def test_recovery_v3_rejects_record_substitution_during_directory_fsync(monkeypatch, tmp_path):
    if sys.platform == "win32":
        _assert_windows_hub_refusal(monkeypatch, tmp_path, operation="storage")
        return
    from brigade import dirfd

    path = codex_cloud._lease_recovery_path(tmp_path, "lease-1")
    path.parent.parent.mkdir(parents=True)
    victim = tmp_path / "victim.json"
    victim.write_bytes(_recovery_record_bytes())
    victim.chmod(0o600)
    original_sync = dirfd.fsync_directory
    substituted = False

    def swap_record(descriptor):
        nonlocal substituted
        info = codex_cloud.os.fstat(descriptor)
        if path.exists() and not substituted:
            parent = path.parent.stat()
            if (info.st_dev, info.st_ino) == (parent.st_dev, parent.st_ino):
                path.unlink()
                path.symlink_to(victim)
                substituted = True
        original_sync(descriptor)

    monkeypatch.setattr(dirfd, "fsync_directory", swap_record)
    with pytest.raises(OSError):
        codex_cloud._persist_lease_holder(tmp_path, "lease-1", "h1")
    assert substituted
    assert victim.read_bytes() == _recovery_record_bytes()
    assert codex_cloud._recover_lease_holder(tmp_path, "lease-1") is None


def test_recovery_v3_pins_directory_during_a_path_swap(monkeypatch, tmp_path):
    if sys.platform == "win32":
        _assert_windows_hub_refusal(monkeypatch, tmp_path, operation="storage")
        return
    from brigade import dirfd

    path = codex_cloud._lease_recovery_path(tmp_path, "lease-1")
    path.parent.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    moved = tmp_path / "moved-private-directory"
    original_open = dirfd.open_child_file
    swapped = False

    def swap_parent(parent, name, flags, mode=0o600, **kwargs):
        nonlocal swapped
        if name == path.name and flags & codex_cloud.os.O_CREAT and not swapped:
            path.parent.rename(moved)
            path.parent.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_open(parent, name, flags, mode, **kwargs)

    monkeypatch.setattr(dirfd, "open_child_file", swap_parent)
    with pytest.raises(OSError):
        codex_cloud._persist_lease_holder(tmp_path, "lease-1", "h1")
    assert swapped
    assert list(outside.iterdir()) == []


def test_recovery_v3_zero_write_cannot_be_recovered(monkeypatch, tmp_path):
    path = codex_cloud._lease_recovery_path(tmp_path, "lease-1")
    path.parent.parent.mkdir(parents=True)
    monkeypatch.setattr(codex_cloud.os, "write", lambda *a, **k: 0)
    with pytest.raises(OSError):
        codex_cloud._persist_lease_holder(tmp_path, "lease-1", "h1")
    assert codex_cloud._recover_lease_holder(tmp_path, "lease-1") is None


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


def test_run_cloud_task_happy_path(monkeypatch, tmp_path):
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
        # The registry lives under cwd; never record this fake task in the real checkout.
        cwd=tmp_path,
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
    def test_admission_uses_canonical_codex_hub_provider(self, monkeypatch, tmp_path):
        if sys.platform == "win32":
            _assert_windows_hub_refusal(monkeypatch, tmp_path, operation="roster")
            return
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

    def test_admission_ordering_and_lifecycle(self, monkeypatch, tmp_path):
        if sys.platform == "win32":
            _assert_windows_hub_refusal(monkeypatch, tmp_path, operation="roster")
            return
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

    def test_admission_denial_makes_zero_provider_calls(self, monkeypatch, tmp_path):
        if sys.platform == "win32":
            _assert_windows_hub_refusal(monkeypatch, tmp_path, operation="roster")
            return
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

    def test_missing_lease_id_makes_zero_provider_calls(self, monkeypatch, tmp_path):
        if sys.platform == "win32":
            _assert_windows_hub_refusal(monkeypatch, tmp_path, operation="roster")
            return
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

    def test_admission_label_uses_sanitized_provider_repo_hash(self, monkeypatch, tmp_path):
        if sys.platform == "win32":
            _assert_windows_hub_refusal(monkeypatch, tmp_path, operation="roster")
            return
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
    def test_ambiguous_submit_holds_lease_and_does_not_release(self, monkeypatch, tmp_path, exec_result):
        if sys.platform == "win32":
            _assert_windows_hub_refusal(monkeypatch, tmp_path, operation="roster")
            return
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

    def test_bind_failure_retains_lease(self, monkeypatch, tmp_path):
        if sys.platform == "win32":
            _assert_windows_hub_refusal(monkeypatch, tmp_path, operation="roster")
            return
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

    def test_terminal_failure_releases_lease(self, monkeypatch, tmp_path):
        if sys.platform == "win32":
            _assert_windows_hub_refusal(monkeypatch, tmp_path, operation="roster")
            return
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

    def test_nonzero_status_with_error_marker_releases_failed_lease(self, monkeypatch, tmp_path):
        if sys.platform == "win32":
            _assert_windows_hub_refusal(monkeypatch, tmp_path, operation="roster")
            return
        recorded = _grant_hub(monkeypatch)
        fake = FakeRuns(
            {
                "exec": [_result(stdout="task_err1")],
                "status": [_result(code=1, stdout="[ERROR] environment setup failed\nbrigade  -  2s ago")],
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
        assert result.status == "error"
        assert recorded["release"]
        assert recorded["release"][0]["args"][0] == "lease-1"
        assert recorded["release"][0]["kwargs"]["state"] == "error"

    def test_unknown_nonzero_status_holds_lease(self, monkeypatch, tmp_path):
        if sys.platform == "win32":
            _assert_windows_hub_refusal(monkeypatch, tmp_path, operation="roster")
            return
        recorded = _grant_hub(monkeypatch)
        fake = FakeRuns(
            {
                "exec": [_result(stdout="task_statnz1")],
                "status": [_result(code=1, stdout="provider dropped the connection")],
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
        assert result.detail == "cloud status unavailable"
        assert recorded["release"] == []

    def test_transient_unknown_nonzero_status_recovers(self, monkeypatch, tmp_path):
        if sys.platform == "win32":
            _assert_windows_hub_refusal(monkeypatch, tmp_path, operation="roster")
            return
        recorded = _grant_hub(monkeypatch)
        slept: list[float] = []
        fake = FakeRuns(
            {
                "exec": [_result(stdout="task_statgrace1")],
                "status": [
                    _result(code=1, stdout="task is not visible yet"),
                    _result(stdout="[PENDING] BRIGADE_CLOUD_MAIN_OK\nno diff"),
                    _result(stdout="[COMPLETED] BRIGADE_CLOUD_MAIN_OK\nno diff"),
                ],
                "diff": [_result(stdout="")],
            }
        )
        monkeypatch.setattr(codex_cloud.proc, "run", fake)
        result = codex_cloud.run_cloud_task(
            "x",
            env_id="e",
            timeout=600,
            poll_interval=codex_cloud.POLL_INTERVAL,
            sleep=slept.append,
            clock=lambda: 0,
        )
        assert result.ok
        assert result.status == "completed"
        assert recorded["release"]
        assert recorded["release"][0]["kwargs"]["state"] == "completed"
        assert slept == [codex_cloud.POLL_INTERVAL, codex_cloud.POLL_INTERVAL]
        assert len(recorded["renew"]) == 2

    def test_nonzero_pending_status_continues_polling(self, monkeypatch, tmp_path):
        if sys.platform == "win32":
            _assert_windows_hub_refusal(monkeypatch, tmp_path, operation="roster")
            return
        recorded = _grant_hub(monkeypatch)
        fake = FakeRuns(
            {
                "exec": [_result(stdout="task_pendingnz1")],
                "status": [
                    _result(code=1, stdout="[PENDING] BRIGADE_CLOUD_MAIN_OK\nno diff"),
                    _result(stdout="[COMPLETED] BRIGADE_CLOUD_MAIN_OK\nno diff"),
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
        assert result.status == "completed"
        assert len(recorded["renew"]) == 1
        assert recorded["release"]

    def test_unknown_nonzero_status_exhausts_grace_then_holds_lease(self, monkeypatch, tmp_path):
        if sys.platform == "win32":
            _assert_windows_hub_refusal(monkeypatch, tmp_path, operation="roster")
            return
        recorded = _grant_hub(monkeypatch)
        fake = FakeRuns(
            {
                "exec": [_result(stdout="task_statnzex1")],
                "status": [_result(code=1, stdout="provider dropped the connection")],
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
        assert result.detail == "cloud status unavailable"
        assert recorded["release"] == []
        status_calls = sum(1 for call in fake.calls if call[2] == "status")
        retries = codex_cloud.MAX_TRANSIENT_STATUS_RETRIES
        assert status_calls == retries + 1
        assert len(recorded["renew"]) == retries

    def test_auth_failure_status_fails_immediately(self, monkeypatch, tmp_path):
        if sys.platform == "win32":
            _assert_windows_hub_refusal(monkeypatch, tmp_path, operation="roster")
            return
        recorded = _grant_hub(monkeypatch)
        fake = FakeRuns(
            {
                "exec": [_result(stdout="task_statauth1")],
                "status": [_result(code=1, stderr="Error: not logged in")],
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
        assert result.failure_kind == "auth-failure"
        assert result.detail == "cloud status authentication failed"
        assert recorded["release"] == []
        assert sum(1 for call in fake.calls if call[2] == "status") == 1
        assert recorded["renew"] == []

    def test_terminal_success_releases_lease_before_diff(self, monkeypatch, tmp_path):
        if sys.platform == "win32":
            _assert_windows_hub_refusal(monkeypatch, tmp_path, operation="roster")
            return
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

    def test_nonterminal_status_polls_renew_lease(self, monkeypatch, tmp_path):
        if sys.platform == "win32":
            _assert_windows_hub_refusal(monkeypatch, tmp_path, operation="roster")
            return
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

    def test_renew_refused_stops_polling_without_release(self, monkeypatch, tmp_path):
        if sys.platform == "win32":
            _assert_windows_hub_refusal(monkeypatch, tmp_path, operation="roster")
            return
        recorded = _grant_hub(monkeypatch)

        def fake_renew(*args, **kwargs):
            recorded["renew"].append({"args": args, "kwargs": kwargs})
            return fleet_client.CloudDecision(False, "refused", holder=kwargs.get("holder"))

        monkeypatch.setattr(fleet_client, "renew_cloud", fake_renew)
        fake = FakeRuns(
            {
                "exec": [_result(stdout="task_renew_refused1")],
                "status": [
                    _result(stdout="Status: running"),
                    _result(stdout="Status: running"),
                ],
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
        assert result.failure_kind == "fleet-lease-lost"
        assert recorded["renew"]
        assert recorded["release"] == []
        assert len(fake.calls) == 2

    def test_consecutive_hub_unavailable_renews_fail_closed_without_duplicate_exec(self, monkeypatch, tmp_path):
        if sys.platform == "win32":
            _assert_windows_hub_refusal(monkeypatch, tmp_path, operation="roster")
            return
        recorded = _grant_hub(monkeypatch)
        renew_calls = {"n": 0}

        def fake_renew(*args, **kwargs):
            renew_calls["n"] += 1
            recorded["renew"].append({"args": args, "kwargs": kwargs})
            return fleet_client.CloudDecision(False, "hub-unavailable", holder=kwargs.get("holder"))

        monkeypatch.setattr(fleet_client, "renew_cloud", fake_renew)
        fake = FakeRuns(
            {
                "exec": [_result(stdout="task_renew_hub1")],
                "status": [_result(stdout="Status: running")],
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
        assert result.failure_kind == "fleet-lease-lost"
        assert renew_calls["n"] == codex_cloud.MAX_CONSECUTIVE_HUB_UNAVAILABLE_RENEWS
        assert sum(1 for call in fake.calls if call[2] == "exec") == 1
        assert recorded["release"] == []

    def test_diff_is_read_only_and_never_applies(self, monkeypatch, tmp_path):
        if sys.platform == "win32":
            _assert_windows_hub_refusal(monkeypatch, tmp_path, operation="roster")
            return
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

    def test_status_timeout_holds_lease_and_returns_uncertain(self, monkeypatch, tmp_path):
        if sys.platform == "win32":
            _assert_windows_hub_refusal(monkeypatch, tmp_path, operation="roster")
            return
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

    def test_unknown_status_renews_lease_until_timeout(self, monkeypatch, tmp_path):
        if sys.platform == "win32":
            _assert_windows_hub_refusal(monkeypatch, tmp_path, operation="roster")
            return
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
        assert len(recorded["renew"]) == 1
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
        assert registrations[0]["label"] == cloud_tracker.lease_label("codex", None, cloud_tracker.prompt_hash(prompt))
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
        tasks = codex_cloud.list_tasks().tasks
        assert tasks == [
            {"id": "task_t01", "state": "running", "environment_id": "env-1"},
            {"id": "task_t02", "state": "completed"},
        ]
        assert "PRIVATE" not in json.dumps(tasks)
        assert "cursor" not in json.dumps(tasks)

    def test_list_tasks_rejects_undocumented_plain_array(self, monkeypatch):
        raw = [{"id": "task_t3", "state": "FAILED", "title": "PRIVATE"}]
        monkeypatch.setattr(codex_cloud.proc, "run", lambda *a, **k: _result(stdout=json.dumps(raw)))
        outcome = codex_cloud.list_tasks()
        assert outcome.tasks == []
        assert outcome.ok is False
        assert outcome.reason == "list-json-unsupported"

    def test_list_tasks_bounds_max_items(self, monkeypatch):
        raw = {"tasks": [{"id": f"task_t0{i}", "status": "RUNNING"} for i in range(5)]}
        monkeypatch.setattr(codex_cloud.proc, "run", lambda *a, **k: _result(stdout=json.dumps(raw)))
        tasks = codex_cloud.list_tasks(max_items=2).tasks
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
        assert codex_cloud.list_tasks(max_items=25).tasks == [
            {"id": "task_page1", "state": "running"},
            {"id": "task_page2", "state": "completed"},
        ]
        assert calls[0][-2:] == ["--limit", "20"]
        assert calls[1][-4:] == ["--limit", "20", "--cursor", "next"]

    def test_list_tasks_max_pages_stops_after_a_short_page(self, monkeypatch):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return _result(stdout=json.dumps({"tasks": [{"id": "task_page1", "status": "RUNNING"}], "cursor": "next"}))

        monkeypatch.setattr(codex_cloud.proc, "run", fake_run)
        assert codex_cloud.list_tasks(max_items=20, max_pages=1).tasks == [{"id": "task_page1", "state": "running"}]
        assert len(calls) == 1
        assert "--cursor" not in calls[0]

    def test_list_tasks_returns_failure_reason_on_command_failure(self, monkeypatch):
        monkeypatch.setattr(codex_cloud.proc, "run", lambda *a, **k: _result(code=1, stderr="boom"))
        outcome = codex_cloud.list_tasks()
        assert outcome.tasks == []
        assert outcome.ok is False
        assert outcome.reason == "provider-error"

    def test_list_tasks_reports_missing_provider(self, monkeypatch):
        monkeypatch.setattr(
            codex_cloud.proc,
            "run",
            lambda *a, **k: _result(code=127, stderr="command not found: codex"),
        )
        outcome = codex_cloud.list_tasks()
        assert outcome.ok is False
        assert outcome.reason == "provider-missing"

    def test_list_tasks_reports_auth_failure(self, monkeypatch):
        monkeypatch.setattr(
            codex_cloud.proc,
            "run",
            lambda *a, **k: _result(code=1, stderr="Error: not logged in to Codex Cloud"),
        )
        outcome = codex_cloud.list_tasks()
        assert outcome.ok is False
        assert outcome.reason == "auth-failure"

    def test_list_tasks_returns_empty_on_non_json_output(self, monkeypatch):
        monkeypatch.setattr(codex_cloud.proc, "run", lambda *a, **k: _result(stdout="not json"))
        outcome = codex_cloud.list_tasks()
        assert outcome.tasks == []
        assert outcome.ok is False
        assert outcome.reason == "list-json-invalid"


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

    def test_config_file_literal_id_branch_stores_value_kind(self, tmp_path):
        path = codex_cloud.save_environment_config(tmp_path, environment_id="env-local-file")
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["environment"] == {"kind": "value", "id": "env-local-file"}
        mode = path.stat().st_mode & 0o777
        if sys.platform == "win32":
            # Windows mode bits describe read/write metadata, not private ACLs.
            assert mode & 0o600 == 0o600
        else:
            assert mode == 0o600

    def test_corrupt_config_fails_closed(self, tmp_path):
        path = codex_cloud.config_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(codex_cloud.CodexCloudConfigError, match="unreadable"):
            codex_cloud.load_environment_config(tmp_path)

    def test_future_schema_config_fails_closed(self, tmp_path):
        path = codex_cloud.config_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"schema": "brigade.codex-cloud-config/2", "environment": {"kind": "value", "id": "x"}}),
            encoding="utf-8",
        )
        with pytest.raises(codex_cloud.CodexCloudConfigError, match="unsupported schema"):
            codex_cloud.load_environment_config(tmp_path)

    def test_resolve_config_target_finds_repo_root_from_subdirectory(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_CLOUD_ENV", "env-from-parent")
        codex_cloud.save_environment_config(tmp_path, environment_id_env="CODEX_CLOUD_ENV")
        subdir = tmp_path / "pkg" / "src"
        subdir.mkdir(parents=True)
        assert codex_cloud.resolve_environment_id("configured", target=subdir) == "env-from-parent"

    def test_environment_audit_uses_fingerprint_for_configured_selector(self):
        audit = codex_cloud.environment_audit_ref("configured", "env-secret-id")
        digest = hashlib.sha256(b"env-secret-id").hexdigest()[:12]
        assert audit == {"environment_fingerprint": f"sha256:{digest}"}
        assert "env-secret-id" not in json.dumps(audit)

    def test_environment_audit_uses_raw_id_for_literal_selector(self):
        audit = codex_cloud.environment_audit_ref("env-123", "env-123")
        assert audit == {"environment_id": "env-123"}

    def test_run_agent_resolves_configured_before_dispatch(self, tmp_path, monkeypatch):
        seen: dict[str, str] = {}

        def fake_cloud_task(prompt, *, env_id, timeout, cwd=None, process_registry=None, **kwargs):
            seen["env_id"] = env_id
            return agents.AgentResult(text="ok", ok=True)

        monkeypatch.setenv("CODEX_CLOUD_ENV", "env-resolved")
        monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command + ".exe")
        monkeypatch.setattr(codex_cloud, "run_cloud_task", fake_cloud_task)
        result = agents.run_agent("codex-cloud:configured", "fix it", cwd=tmp_path)
        assert result.ok
        assert seen["env_id"] == "env-resolved"

    def test_run_agent_unresolved_configured_does_not_dispatch(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.delenv("CODEX_CLOUD_ENV", raising=False)
        monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command + ".exe")
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
        assert "environment_fingerprint" in report
        assert "env-canary" not in json.dumps(report)
        codex_calls = [call for call in fake.calls if call[:2] == ["codex", "cloud"]]
        assert all(call[2] == "list" for call in codex_calls)
        assert codex_calls

    def test_canary_empty_inventory_passes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_CLOUD_ENV", "env-canary")
        monkeypatch.setattr(
            codex_cloud.proc,
            "run",
            lambda *a, **k: _result(stdout=json.dumps({"tasks": []})),
        )
        report = codex_cloud.canary(tmp_path)
        assert report["ok"] is True
        assert report["task_count"] == 0

    def test_canary_reports_environment_seen_when_inventory_matches(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_CLOUD_ENV", "env-canary")
        monkeypatch.setattr(
            codex_cloud.proc,
            "run",
            lambda *a, **k: _result(
                stdout=json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "task_c01",
                                "status": "COMPLETED",
                                "environment_id": "env-canary",
                            }
                        ]
                    }
                )
            ),
        )
        report = codex_cloud.canary(tmp_path)
        assert report["ok"] is True
        assert report["environment_seen"] == "true"
        assert "reason" not in report

    def test_canary_reports_environment_not_seen_when_inventory_differs(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_CLOUD_ENV", "env-canary")
        monkeypatch.setattr(
            codex_cloud.proc,
            "run",
            lambda *a, **k: _result(
                stdout=json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "task_c01",
                                "status": "COMPLETED",
                                "environment_id": "env-other",
                            }
                        ]
                    }
                )
            ),
        )
        report = codex_cloud.canary(tmp_path)
        assert report["ok"] is True
        assert report["environment_seen"] == "false"
        assert report["reason"] == "environment-not-seen"

    def test_canary_empty_inventory_leaves_environment_seen_unknown(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_CLOUD_ENV", "env-canary")
        monkeypatch.setattr(
            codex_cloud.proc,
            "run",
            lambda *a, **k: _result(stdout=json.dumps({"tasks": []})),
        )
        report = codex_cloud.canary(tmp_path)
        assert report["ok"] is True
        assert report["environment_seen"] == "unknown"

    def test_canary_fails_closed_when_provider_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_CLOUD_ENV", "env-canary")
        monkeypatch.setattr(codex_cloud.proc, "run", lambda *a, **k: _result(code=127, stderr="command not found"))
        report = codex_cloud.canary(tmp_path)
        assert report["ok"] is False
        assert report["reason"] == "provider-missing"

    def test_canary_fails_closed_on_auth_failure(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_CLOUD_ENV", "env-canary")
        monkeypatch.setattr(
            codex_cloud.proc,
            "run",
            lambda *a, **k: _result(code=1, stderr="Error: sign in required"),
        )
        report = codex_cloud.canary(tmp_path)
        assert report["ok"] is False
        assert report["reason"] == "auth-failure"

    @pytest.mark.parametrize(
        ("selector", "env_setup"),
        [
            ("configured", lambda monkeypatch, tmp_path: monkeypatch.setenv("CODEX_CLOUD_ENV", "env-seat")),
            ("env-literal", lambda monkeypatch, tmp_path: None),
            ("$CODEX_CLOUD_ENV", lambda monkeypatch, tmp_path: monkeypatch.setenv("CODEX_CLOUD_ENV", "env-seat")),
        ],
    )
    def test_doctor_and_canary_validate_explicit_selector(self, tmp_path, monkeypatch, selector, env_setup):
        env_setup(monkeypatch, tmp_path)
        if selector == "configured":
            codex_cloud.save_environment_config(tmp_path, environment_id_env="CODEX_CLOUD_ENV")
        monkeypatch.setattr(
            codex_cloud.proc,
            "run",
            lambda *a, **k: _result(stdout=json.dumps({"tasks": []})),
        )
        doctor = codex_cloud.doctor(tmp_path, selector=selector)
        canary = codex_cloud.canary(tmp_path, selector=selector)
        assert doctor["ok"] is True
        assert canary["ok"] is True
        assert doctor["selector"] == selector
        assert canary["selector"] == selector

    def test_run_agent_records_environment_audit_on_dispatch(self, tmp_path, monkeypatch):
        seen: dict[str, object] = {}
        codex_cloud.save_environment_config(tmp_path, environment_id_env="CODEX_CLOUD_ENV")
        subdir = tmp_path / "pkg"
        subdir.mkdir()

        def fake_cloud_task(
            prompt,
            *,
            env_id,
            timeout,
            cwd=None,
            process_registry=None,
            environment_audit=None,
            register_target=None,
            **kwargs,
        ):
            seen["environment_audit"] = environment_audit
            seen["register_target"] = register_target
            return agents.AgentResult(text="ok", ok=True, cloud_environment=environment_audit)

        monkeypatch.setenv("CODEX_CLOUD_ENV", "env-resolved")
        monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command + ".exe")
        monkeypatch.setattr(codex_cloud, "run_cloud_task", fake_cloud_task)
        result = agents.run_agent("codex-cloud:configured", "fix it", cwd=subdir)
        assert result.ok
        assert seen["register_target"] == tmp_path.resolve()
        assert seen["environment_audit"] == codex_cloud.environment_audit_ref("configured", "env-resolved")
        assert result.cloud_environment == seen["environment_audit"]

    def test_run_agent_blocks_read_only_without_cloud_safe_mode(self, monkeypatch):
        monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command + ".exe")
        result = agents.run_agent("codex-cloud:env-123", "fix it", read_only=True)
        assert not result.ok
        assert result.failure_kind == "read-only-cloud-blocked"

    def test_run_agent_allows_read_only_with_cloud_safe_mode(self, monkeypatch):
        monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command + ".exe")
        monkeypatch.setattr(
            codex_cloud,
            "run_cloud_task",
            lambda *a, **k: agents.AgentResult(text="ok", ok=True),
        )
        result = agents.run_agent(
            "codex-cloud:env-123",
            "fix it",
            read_only=True,
            cloud_safe_mode=True,
        )
        assert result.ok


class TestSelectorValidation:
    @pytest.mark.parametrize(
        "token",
        [
            " env-1 ",
            "--env",
            "a\nb",
            "",
        ],
    )
    def test_validate_selector_token_rejects_invalid_literals(self, token):
        with pytest.raises(codex_cloud.CodexCloudConfigError):
            codex_cloud.validate_selector_token(token)

    def test_validate_selector_token_accepts_reviewed_literal(self):
        codex_cloud.validate_selector_token("env-123")


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
        codex_calls = [argv for argv in calls if argv[:2] == ["codex", "cloud"]]
        assert all(argv[2] == "list" for argv in codex_calls)
        assert codex_calls
        assert not any(argv[2] in {"exec", "apply"} for argv in calls)

    def test_cloud_doctor_and_canary_route_to_supported_provider_adapters(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("CURSOR_API_KEY", "cursor-key")
        monkeypatch.setenv("JULES_API_KEY", "jules-key")
        monkeypatch.setattr(cursor_cloud, "list_agents", lambda *a, **k: [])
        monkeypatch.setattr(jules_cloud, "list_sessions", lambda *a, **k: [{"id": "sess-1", "state": "running"}])
        whoami_calls = []
        monkeypatch.setattr(
            fleet_client_grokbot,
            "whoami",
            lambda **k: whoami_calls.append(k) or fleet_client_grokbot.GrokbotHubDecision(True, "ok"),
        )
        monkeypatch.setattr(
            fleet_client_grokbot,
            "list_jobs",
            lambda **k: fleet_client_grokbot.GrokbotHubDecision(
                True,
                "ok",
                jobs=[
                    {"job_id": "grokbot-active", "state": "running", "role": "implementation-worker"},
                    {"job_id": "grokbot-done", "state": "completed", "role": "implementation-worker"},
                ],
            ),
        )

        assert (
            cli.main(["run", "cloud", "doctor", "--provider", "cursor-cloud", "--target", str(tmp_path), "--json"]) == 0
        )
        assert json.loads(capsys.readouterr().out)["provider"] == "cursor-cloud"
        assert cli.main(["run", "cloud", "canary", "--provider", "jules", "--target", str(tmp_path), "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["task_count"] == 1
        assert (
            cli.main(["run", "cloud", "doctor", "--provider", "grokbot-cloud", "--target", str(tmp_path), "--json"])
            == 0
        )
        assert whoami_calls
        capsys.readouterr()
        assert (
            cli.main(["run", "cloud", "canary", "--provider", "grokbot-cloud", "--target", str(tmp_path), "--json"])
            == 0
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["tasks"] == [{"job_id": "grokbot-active", "role": "implementation-worker", "state": "running"}]
        assert (
            cli.main(["run", "cloud", "doctor", "--provider", "claude-cloud", "--target", str(tmp_path), "--json"]) == 1
        )
        assert json.loads(capsys.readouterr().out)["reason"] == "disabled-by-policy"
        with pytest.raises(SystemExit) as rejected_setup:
            cli.main(
                ["run", "cloud", "setup", "--provider", "cursor-cloud", "--target", str(tmp_path), "--env-id", "env-1"]
            )
        assert rejected_setup.value.code == 2

    def test_claude_cloud_doctor_is_static_disabled_policy(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(
            cursor_cloud,
            "list_agents",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("cursor must not run")),
        )
        monkeypatch.setattr(
            jules_cloud,
            "list_sessions",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("jules must not run")),
        )
        monkeypatch.setattr(
            fleet_client_grokbot,
            "whoami",
            lambda **k: (_ for _ in ()).throw(AssertionError("grokbot must not run")),
        )
        monkeypatch.setattr(
            codex_cloud,
            "list_tasks",
            lambda **k: (_ for _ in ()).throw(AssertionError("codex must not run")),
        )

        assert (
            cli.main(
                [
                    "run",
                    "cloud",
                    "doctor",
                    "--provider",
                    "claude-cloud",
                    "--target",
                    str(tmp_path),
                    "--json",
                ]
            )
            == 1
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["reason"] == "disabled-by-policy"


@pytest.mark.parametrize("operation", ["launch", "register", "persist", "recover", "directory"])
def test_windows_hub_storage_refuses_before_any_io(monkeypatch, tmp_path, operation):
    from types import SimpleNamespace

    calls = _grant_hub(monkeypatch)
    monkeypatch.setattr(codex_cloud, "sys", SimpleNamespace(platform="win32"))

    def forbidden(*args, **kwargs):
        pytest.fail("unsupported storage performed I/O")

    monkeypatch.setattr(codex_cloud, "_preflight_registry", forbidden)
    monkeypatch.setattr(codex_cloud, "_lease_recovery_path", forbidden)
    monkeypatch.setattr(codex_cloud.proc, "run", forbidden)
    monkeypatch.setattr(cloud_tracker, "load_registry", forbidden)
    monkeypatch.setattr(fleet_client, "fetch_cloud", forbidden)
    if operation == "launch":
        assert codex_cloud.launch_task("private prompt", target=tmp_path, repo="owner/repo", label="test") == {
            "ok": False,
            "reason": "secure-owner-store-unavailable",
        }
    elif operation == "recover":
        assert codex_cloud._recover_lease_holder(tmp_path, "lease-1") is None
    else:
        with pytest.raises(OSError, match="^secure-owner-store-unavailable$"):
            if operation == "register":
                codex_cloud.acquire_registered_task_lease(
                    tmp_path, task_id="task_windows99", label="test", prompt_hash=None, repo="owner/repo"
                )
            elif operation == "persist":
                codex_cloud._persist_lease_holder(tmp_path, "lease-1", "private-holder")
            else:
                with codex_cloud._lease_recovery_directory(tmp_path, create=True):
                    pytest.fail("unsupported directory opened")
    assert all(not value for value in calls.values())
    assert not (tmp_path / ".brigade").exists()


@pytest.mark.parametrize("command", ["launch", "register"])
@pytest.mark.parametrize("as_json", [True, False])
def test_windows_hub_cli_refuses_before_prompt_or_registry(monkeypatch, tmp_path, capsys, command, as_json):
    from types import SimpleNamespace

    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    calls = _grant_hub(monkeypatch)
    monkeypatch.setattr(codex_cloud, "sys", SimpleNamespace(platform="win32"))

    def forbidden(*args, **kwargs):
        pytest.fail("Windows Hub CLI reached provider or registry")

    monkeypatch.setattr(codex_cloud.proc, "run", forbidden)
    monkeypatch.setattr(cloud_tracker, "load_registry", forbidden)
    monkeypatch.setattr(cloud_tracker, "register", forbidden)
    argv = ["run", "cloud", command, "--target", str(tmp_path), "--provider", "codex-cloud", "--label", "test"]
    argv += (
        ["--repo", "owner/repo", "--prompt-file", str(tmp_path / "missing")]
        if command == "launch"
        else ["--task-id", "task_windows99"]
    )
    assert cli.main(argv + (["--json"] if as_json else [])) == 2
    captured = capsys.readouterr()
    assert "secure-owner-store-unavailable" in captured.out + captured.err
    assert all(not value for value in calls.values())
    assert not (tmp_path / ".brigade").exists()


@pytest.mark.parametrize("command", ["launch", "register"])
def test_windows_no_hub_keeps_local_launch_and_registration(monkeypatch, tmp_path, capsys, command):
    from types import SimpleNamespace

    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    codex_cloud.save_environment_config(tmp_path, environment_id="env-local")
    calls = _grant_hub(monkeypatch)
    monkeypatch.setattr(codex_cloud, "_hub_configured", lambda: False)
    monkeypatch.setattr(codex_cloud, "sys", SimpleNamespace(platform="win32"))

    def forbidden(*args, **kwargs):
        pytest.fail("no-Hub path reached private capability storage")

    monkeypatch.setattr(codex_cloud, "_persist_lease_holder", forbidden)
    monkeypatch.setattr(codex_cloud, "_recover_lease_holder", forbidden)
    submissions = []

    def fake_run(argv, **kwargs):
        if argv[:3] != ["codex", "cloud", "exec"]:
            return _result(code=1)
        submissions.append(kwargs["stdin"])
        return _result(stdout="task_windows99")

    monkeypatch.setattr(codex_cloud.proc, "run", fake_run)
    if command == "launch":
        result = codex_cloud.launch_task("private prompt", target=tmp_path, repo="owner/repo", label="test")
        assert result["ok"] is True
        assert submissions == [b"private prompt"]
    else:
        assert (
            cli.main(
                [
                    "run",
                    "cloud",
                    "register",
                    "--target",
                    str(tmp_path),
                    "--provider",
                    "codex-cloud",
                    "--label",
                    "test",
                    "--task-id",
                    "task_windows99",
                    "--json",
                ]
            )
            == 0
        )
        assert "lease_holder" not in json.loads(capsys.readouterr().out)
    entry = cloud_tracker.load_registry(tmp_path)["entries"][0]
    assert entry["task_id"] == "task_windows99"
    assert not entry.get("lease_holder")
    assert all(not value for value in calls.values())
    assert not (cloud_tracker.registry_path(tmp_path).parent / "codex-leases").exists()


@pytest.mark.parametrize("through_agents", [False, True])
@pytest.mark.parametrize("hub_configured", [False, True])
def test_windows_roster_dispatch_obeys_hub_storage_policy(monkeypatch, tmp_path, through_agents, hub_configured):
    from types import SimpleNamespace

    calls = _grant_hub(monkeypatch)
    monkeypatch.setattr(codex_cloud, "_hub_configured", lambda: hub_configured)
    monkeypatch.setattr(codex_cloud, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(agents.proc, "which", lambda command: "/fake/codex.exe")
    monkeypatch.setattr(codex_cloud, "resolve_environment_id", lambda *a, **k: "env-local")
    provider = FakeRuns(
        {
            "exec": [_result(stdout="task_windows99")],
            "status": [_result(stdout="Status: completed")],
            "diff": [_result(stdout="fake diff")],
        }
    )

    def fake_run(argv, **kwargs):
        if argv[:2] != ["codex", "cloud"]:
            return _result(code=1)
        assert not hub_configured, "Windows Hub roster dispatch invoked provider"
        return provider(argv, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("Windows Hub roster dispatch reached credential storage")

    monkeypatch.setattr(codex_cloud.proc, "run", fake_run)
    monkeypatch.setattr(codex_cloud, "_persist_lease_holder", forbidden)
    monkeypatch.setattr(codex_cloud, "_recover_lease_holder", forbidden)
    if hub_configured:
        monkeypatch.setattr(cloud_tracker, "register", forbidden)
    if through_agents:
        result = agents.run_agent("codex-cloud:env-local", "private prompt", cwd=tmp_path, timeout=1)
    else:
        result = codex_cloud.run_cloud_task("private prompt", env_id="env-local", timeout=1, register_target=tmp_path)
    assert all(not values for values in calls.values())
    assert not (cloud_tracker.registry_path(tmp_path).parent / "codex-leases").exists()
    if hub_configured:
        assert not result.ok
        assert result.status == "denied"
        assert result.failure_kind == result.detail == "secure-owner-store-unavailable"
        assert provider.calls == []
        assert not (tmp_path / ".brigade").exists()
    else:
        assert result.ok
        assert result.thread_id == "task_windows99"
        assert [argv[2] for argv in provider.calls] == ["exec", "status", "diff"]
        entry = cloud_tracker.load_registry(tmp_path)["entries"][0]
        assert not entry.get("lease_holder")


def test_cli_codex_prompt_without_nofollow_refuses_before_submission(monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace

    from brigade.cli import run_cloud

    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    calls = _grant_hub(monkeypatch)
    monkeypatch.setattr(codex_cloud, "_hub_configured", lambda: False)
    monkeypatch.setattr(run_cloud, "os", SimpleNamespace(O_RDONLY=codex_cloud.os.O_RDONLY, O_NOFOLLOW=0))

    def forbidden(*args, **kwargs):
        pytest.fail("unsafe prompt reader reached submission")

    monkeypatch.setattr(codex_cloud, "launch_task", forbidden)
    assert (
        cli.main(
            [
                "run",
                "cloud",
                "launch",
                "--target",
                str(tmp_path),
                "--provider",
                "codex-cloud",
                "--repo",
                "owner/repo",
                "--label",
                "test",
                "--prompt-file",
                str(tmp_path / "prompt.txt"),
                "--json",
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["reason"] == "bad-prompt-file"
    assert all(not value for value in calls.values())
    assert not (tmp_path / ".brigade").exists()
