"""Tests for the first-party Cerebro Memory connector adapter."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from brigade import grokbot_cerebro, grokbot_ops, grokbot_packs
from tests.test_grokbot_ops import assert_listener_recovery_policy

SECRET_BIN = "/var/lib/secret-cerebro/bin"
SECRET_CWD = "/var/lib/secret-cerebro/work"
SECRET_STDERR = "PRIVATE_STDERR"
SECRET_STDOUT = "PRIVATE_STDOUT token=secret-stdout"
SECRET_HOME = "/home/secret-cerebro"
SECRET_PATH = "/opt/secret-cerebro/bin"
HASH = f"sha256:{'a' * 64}"
NOTE_ID = "note-6a6f1603-c279-4bf8-9462-1467f7cb177b"
UNTRUSTED = grokbot_cerebro.UNTRUSTED_VAULT_CONTENT


def _reject(code: str, message: str | None = None):
    return pytest.raises(grokbot_cerebro.CerebroError, match=message or grokbot_cerebro.ERROR_MESSAGES[code])


def _hit(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": NOTE_ID,
        "title": "Inbox",
        "scope": "agent-inbox",
        "relative_path": "00 - Inbox/Agent Notes/inbox.md",
        "content_hash": HASH,
        "updated_at": "2026-08-21T00:00:00Z",
        "tags": ["agent"],
        "snippet": "cited",
        "score": 8,
        "trust": UNTRUSTED,
    }
    payload.update(overrides)
    return payload


def _note(**overrides: object) -> dict[str, object]:
    payload = _hit()
    payload["text"] = "cited"
    payload.update(overrides)
    return payload


class _FakeRunner:
    def __init__(self, impl=None):
        self.calls: list[grokbot_cerebro.ExecRequest] = []
        self._impl = impl

    def __call__(self, request: grokbot_cerebro.ExecRequest) -> grokbot_cerebro.PrivateExecResult:
        self.calls.append(request)
        if self._impl is not None:
            return self._impl(request)
        if request.args and request.args[0] == "show":
            return grokbot_cerebro.PrivateExecResult(
                stdout=json.dumps({"schema": "cerebro-agents.show.v1", "note": _note()})
            )
        return grokbot_cerebro.PrivateExecResult(
            stdout=json.dumps({"schema": "cerebro-agents.search.v1", "results": []})
        )


def _tools(impl=None) -> tuple[grokbot_cerebro.CerebroTools, _FakeRunner]:
    runner = _FakeRunner(impl)
    tools = grokbot_cerebro.CerebroTools(
        cli_executable=SECRET_BIN,
        workdir=SECRET_CWD,
        env={
            "PATH": SECRET_PATH,
            "HOME": SECRET_HOME,
            "GROKBOT_CEREBRO_TOKEN": "C" * 32,
            "UNRELATED": "must-not-copy",
        },
        runner=runner,
    )
    return tools, runner


def _secrets() -> list[str]:
    return [SECRET_BIN, SECRET_CWD, SECRET_STDERR, SECRET_STDOUT, SECRET_HOME, SECRET_PATH]


def _assert_hidden(value: object, *extra: str) -> None:
    rendered = value if isinstance(value, str) else json.dumps(value, default=str)
    for secret in (*_secrets(), *extra):
        assert secret not in rendered


def test_search_defaults_limit_and_rejects_out_of_range_or_extra_fields():
    assert grokbot_cerebro.parse_search_input({"query": "alpha"}) == {"query": "alpha", "limit": 5}
    assert grokbot_cerebro.parse_search_input({"query": "alpha", "scope": "agent-inbox", "limit": 10}) == {
        "query": "alpha",
        "scope": "agent-inbox",
        "limit": 10,
    }
    for raw in (
        {"query": ""},
        {"query": "a" * 513},
        {"query": "alpha", "limit": 0},
        {"query": "alpha", "limit": 11},
        {"query": "alpha", "scope": "brigade-memory"},
        {"query": "alpha", "include_archived": True},
        {"query": "alpha", "path": "/vault"},
        {"query": "alpha", "limit": True},
    ):
        with _reject("invalid_request"):
            grokbot_cerebro.parse_search_input(raw)


def test_input_limits_use_utf8_bytes_and_reject_command_path_env_fields():
    emoji = "\U0001f600"
    grokbot_cerebro.parse_search_input({"query": "a" * 512})
    with _reject("invalid_request"):
        grokbot_cerebro.parse_search_input({"query": "a" * 513})
    grokbot_cerebro.parse_search_input({"query": emoji * 128})
    with _reject("invalid_request"):
        grokbot_cerebro.parse_search_input({"query": emoji * 129})

    propose = {
        "request_id": "req-1234",
        "title": "Reconcile",
        "body": "a" * 12288,
        "source_refs": [{"note_id": "note-alpha", "content_hash": HASH}],
    }
    grokbot_cerebro.parse_propose_input(propose)
    with _reject("invalid_request"):
        grokbot_cerebro.parse_propose_input({**propose, "body": "a" * 12289})
    grokbot_cerebro.parse_propose_input({**propose, "body": emoji * 3072})
    with _reject("invalid_request"):
        grokbot_cerebro.parse_propose_input({**propose, "body": emoji * 3073})

    grokbot_cerebro.parse_show_input({"note_id": NOTE_ID})
    grokbot_cerebro.parse_status_input({"request_id": "req-1234"})
    grokbot_cerebro.parse_health_input({})
    for extra in ({"command": "id"}, {"path": "/vault"}, {"env": {}}, {"approval": True}, {"argv": []}):
        with _reject("invalid_request"):
            grokbot_cerebro.parse_show_input({"note_id": "note-alpha", **extra})
        with _reject("invalid_request"):
            grokbot_cerebro.parse_health_input(extra)


def test_truncate_utf8_does_not_split_a_four_byte_emoji():
    prefix = "a" * 65535
    text = grokbot_cerebro.truncate_utf8(prefix + "\U0001f600" + "suffix", 65536)
    assert text == prefix
    assert grokbot_cerebro.utf8_byte_length(text) == 65535
    assert "\ufffd" not in text


def test_executor_uses_shell_false_allowlist_timeout_and_hides_child_output():
    recorded: list[grokbot_cerebro.ExecRequest] = []

    def runner(request: grokbot_cerebro.ExecRequest) -> grokbot_cerebro.PrivateExecResult:
        recorded.append(request)
        return grokbot_cerebro.PrivateExecResult(stdout=SECRET_STDOUT)

    result = grokbot_cerebro.run_exec(
        grokbot_cerebro.ExecRequest(
            file=SECRET_BIN,
            args=("search", "--json", "--", "alpha"),
            cwd=SECRET_CWD,
            timeout_ms=grokbot_cerebro.EXEC_DEFAULT_TIMEOUT_MS,
            max_buffer_bytes=grokbot_cerebro.EXEC_DEFAULT_OUTPUT_BYTES,
            env={
                "PATH": SECRET_PATH,
                "HOME": SECRET_HOME,
                "USER": "cerebro",
                "GROKBOT_CEREBRO_TOKEN": "C" * 32,
                "UNRELATED": "must-not-copy",
            },
            stdin=b"Cited note.\n",
        ),
        runner=runner,
    )
    assert result.stdout == SECRET_STDOUT
    assert not hasattr(result, "stderr")
    assert recorded[0].shell is False
    assert recorded[0].timeout_ms == 15_000
    assert recorded[0].max_buffer_bytes == 65_536
    assert recorded[0].stdin == b"Cited note.\n"
    assert set(recorded[0].env) <= set(grokbot_cerebro.CHILD_ENVIRONMENT_KEYS)
    assert "GROKBOT_CEREBRO_TOKEN" not in recorded[0].env
    assert "UNRELATED" not in recorded[0].env


def test_executor_maps_failures_to_stable_errors_without_paths_or_stderr():
    def timeout(_request: grokbot_cerebro.ExecRequest) -> grokbot_cerebro.PrivateExecResult:
        raise grokbot_cerebro.ChildTimeout()

    with pytest.raises(grokbot_cerebro.CerebroError) as caught:
        grokbot_cerebro.run_exec(
            grokbot_cerebro.ExecRequest(
                file=SECRET_BIN,
                args=("search",),
                cwd=SECRET_CWD,
                timeout_ms=15_000,
                max_buffer_bytes=65_536,
                env={"PATH": SECRET_PATH},
            ),
            runner=timeout,
        )
    assert caught.value.code == "timeout"
    assert caught.value.message == "Cerebro observation timed out"
    _assert_hidden(str(caught.value), "ETIMEDOUT", "spawn")

    def oversize(_request: grokbot_cerebro.ExecRequest) -> grokbot_cerebro.PrivateExecResult:
        raise grokbot_cerebro.ChildOversize()

    with pytest.raises(grokbot_cerebro.CerebroError) as caught:
        grokbot_cerebro.run_exec(
            grokbot_cerebro.ExecRequest(
                file=SECRET_BIN,
                args=("search",),
                cwd=SECRET_CWD,
                timeout_ms=15_000,
                max_buffer_bytes=65_536,
                env={},
            ),
            runner=oversize,
        )
    assert caught.value.code == "protocol_error"
    assert caught.value.message == "Cerebro observation was invalid"
    _assert_hidden(str(caught.value), SECRET_STDERR)

    def missing(_request: grokbot_cerebro.ExecRequest) -> grokbot_cerebro.PrivateExecResult:
        raise OSError("spawn /var/lib/secret-cerebro/bin ENOENT")

    with pytest.raises(grokbot_cerebro.CerebroError) as caught:
        grokbot_cerebro.run_exec(
            grokbot_cerebro.ExecRequest(
                file=SECRET_BIN,
                args=("search",),
                cwd=SECRET_CWD,
                timeout_ms=15_000,
                max_buffer_bytes=65_536,
                env={},
            ),
            runner=missing,
        )
    assert caught.value.code == "unavailable"
    _assert_hidden(str(caught.value), "ENOENT")

    with pytest.raises(grokbot_cerebro.CerebroError) as caught:
        grokbot_cerebro.run_exec(
            grokbot_cerebro.ExecRequest(
                file=SECRET_BIN,
                args=("search",),
                cwd=SECRET_CWD,
                timeout_ms=249,
                max_buffer_bytes=65_536,
                env={},
            ),
            runner=lambda _request: grokbot_cerebro.PrivateExecResult(stdout=""),
        )
    assert caught.value.code == "invalid_request"
    assert caught.value.message == "Cerebro execution request is invalid"


def test_tools_filter_allowed_scopes_and_deny_show_outside_them():
    def impl(request: grokbot_cerebro.ExecRequest) -> grokbot_cerebro.PrivateExecResult:
        if request.args[0] == "search":
            return grokbot_cerebro.PrivateExecResult(
                stdout=json.dumps(
                    {
                        "schema": "cerebro-agents.search.v1",
                        "results": [
                            _hit(),
                            _hit(id="note-hidden", scope="other", title="Hidden"),
                            _hit(id="note-work", scope="agent-work-log", title="Work"),
                        ],
                    }
                )
            )
        return grokbot_cerebro.PrivateExecResult(
            stdout=json.dumps(
                {
                    "schema": "cerebro-agents.show.v1",
                    "note": _note(scope="brigade-memory", text="SECRET_NOTE_TEXT"),
                }
            )
        )

    tools, runner = _tools(impl)
    result = tools.search({"query": "handoff"})
    assert [item["scope"] for item in result["results"]] == ["agent-inbox", "agent-work-log"]
    _assert_hidden(result, "Hidden", "SECRET_NOTE_TEXT")
    assert runner.calls[0].args[:2] == ("search", "--json")

    with pytest.raises(grokbot_cerebro.CerebroError) as caught:
        tools.show({"note_id": NOTE_ID})
    assert caught.value.code == "denied"
    assert caught.value.message == "Cerebro request was denied"
    _assert_hidden(str(caught.value), "SECRET_NOTE_TEXT")


def test_tools_project_public_propose_and_map_conflict_and_health():
    propose_count = 0

    def impl(request: grokbot_cerebro.ExecRequest) -> grokbot_cerebro.PrivateExecResult:
        nonlocal propose_count
        if request.args[0] == "propose":
            propose_count += 1
            if propose_count == 3:
                raise grokbot_cerebro.ChildFailure(2, b"", b"idempotency conflict\n")
            return grokbot_cerebro.PrivateExecResult(
                stdout=json.dumps(
                    {
                        "schema": "cerebro-agents.proposal.v1",
                        "request_id": "req-1234",
                        "id": NOTE_ID,
                        "delivered": True,
                        "duplicate": propose_count > 1,
                        "phase": "terminal",
                        "delivery_state": "delivered",
                        "receipt_ref": "c" * 32,
                    }
                )
            )
        if request.args[0] == "doctor":
            return grokbot_cerebro.PrivateExecResult(
                stdout=json.dumps(
                    {
                        "schema": "cerebro-agents.doctor.v1",
                        "ok": True,
                        "summary": {
                            "cli": "ok",
                            "configuration": "ok",
                            "index": "ok",
                            "proposal_bridge": "ok",
                            "index_age_seconds": 9,
                        },
                        "checks": [{"name": "vault", "status": "ok", "message": SECRET_BIN}],
                    }
                )
            )
        raise AssertionError(request.args)

    tools, runner = _tools(impl)
    payload = {
        "request_id": "req-1234",
        "title": "Reconcile",
        "body": "Cited note.\n",
        "source_refs": [{"note_id": "note-alpha", "content_hash": HASH}],
    }
    first = tools.propose(payload)
    second = tools.propose(payload)
    assert first == {
        "request_id": "req-1234",
        "proposal_id": NOTE_ID,
        "delivery_state": "delivered",
        "duplicate": False,
        "receipt_ref": "c" * 32,
    }
    assert second["duplicate"] is True
    assert set(first) == {"request_id", "proposal_id", "delivery_state", "duplicate", "receipt_ref"}
    assert runner.calls[0].args == (
        "propose",
        "--request-id",
        "req-1234",
        "--title",
        "Reconcile",
        "--json",
        "--source-ref",
        f"{HASH}:note-alpha",
    )
    assert runner.calls[0].stdin == b"Cited note.\n"

    with pytest.raises(grokbot_cerebro.CerebroError) as caught:
        tools.propose(payload)
    assert caught.value.code == "conflict"
    _assert_hidden(str(caught.value))

    with pytest.raises(grokbot_cerebro.CerebroError):
        tools.search({"query": "", "path": "/vault"})
    assert all(call.args[0] != "search" for call in runner.calls)

    health = tools.health({})
    assert health == {
        "ok": True,
        "summary": {
            "cli": "ok",
            "configuration": "ok",
            "index": "ok",
            "proposal_bridge": "ok",
            "index_age_seconds": 9,
        },
    }
    _assert_hidden(health, "checks")


def test_cli_pins_argv_and_rejects_unsafe_relative_paths_without_echoing_them():
    tools, runner = _tools()
    tools.search({"query": "-literal", "limit": 5, "scope": "agent-inbox"})
    tools.show({"note_id": NOTE_ID})
    assert runner.calls[0].args == (
        "search",
        "--json",
        "--limit",
        "5",
        "--scope",
        "agent-inbox",
        "--",
        "-literal",
    )
    assert runner.calls[1].args == ("show", "--json", "--", NOTE_ID)
    assert runner.calls[0].shell is False

    for relative_path in (
        "/tmp/attacker-controlled-path",
        "../secret",
        "foo/../../etc/passwd",
        "C:/Windows/note.md",
        "%2e%2e/%2e%2e/etc/passwd",
    ):

        def impl(request: grokbot_cerebro.ExecRequest, path: str = relative_path) -> grokbot_cerebro.PrivateExecResult:
            return grokbot_cerebro.PrivateExecResult(
                stdout=json.dumps({"schema": "cerebro-agents.search.v1", "results": [_hit(relative_path=path)]})
            )

        leaking, _ = _tools(impl)
        with pytest.raises(grokbot_cerebro.CerebroError) as caught:
            leaking.search({"query": "handoff"})
        assert caught.value.code == "protocol_error"
        _assert_hidden(str(caught.value), relative_path)


def test_show_truncates_on_utf8_boundary_and_rewrites_trust():
    prefix = "a" * 65535

    def impl(_request: grokbot_cerebro.ExecRequest) -> grokbot_cerebro.PrivateExecResult:
        return grokbot_cerebro.PrivateExecResult(
            stdout=json.dumps(
                {
                    "schema": "cerebro-agents.show.v1",
                    "note": _note(text=prefix + "\U0001f600" + "suffix", status="active"),
                }
            )
        )

    tools, _ = _tools(impl)
    shown = tools.show({"note_id": NOTE_ID})
    assert shown["text"] == prefix
    assert shown["truncated"] is True
    assert shown["trust"] == UNTRUSTED
    assert "status" not in shown


def test_propose_accepts_exit_5_delivery_failure_and_rejects_in_progress():
    def failed(_request: grokbot_cerebro.ExecRequest) -> grokbot_cerebro.PrivateExecResult:
        raise grokbot_cerebro.ChildFailure(
            5,
            json.dumps(
                {
                    "schema": "cerebro-agents.proposal.v1",
                    "request_id": "req-1234",
                    "id": NOTE_ID,
                    "delivered": False,
                    "duplicate": False,
                    "phase": "terminal",
                    "delivery_state": "delivery_failed_retained",
                    "receipt_ref": "b" * 32,
                }
            ).encode("utf-8"),
            SECRET_STDERR.encode("utf-8"),
        )

    tools, _ = _tools(failed)
    result = tools.propose(
        {
            "request_id": "req-1234",
            "title": "Reconcile",
            "body": "Cited note.\n",
            "source_refs": [],
        }
    )
    assert result["delivery_state"] == "delivery_failed_retained"
    assert result["duplicate"] is False
    _assert_hidden(result)

    def in_progress(_request: grokbot_cerebro.ExecRequest) -> grokbot_cerebro.PrivateExecResult:
        return grokbot_cerebro.PrivateExecResult(
            stdout=json.dumps(
                {
                    "schema": "cerebro-agents.proposal.v1",
                    "request_id": "req-1234",
                    "id": NOTE_ID,
                    "delivered": False,
                    "duplicate": False,
                    "phase": "in_progress",
                }
            )
        )

    progressing, _ = _tools(in_progress)
    with pytest.raises(grokbot_cerebro.CerebroError) as caught:
        progressing.propose(
            {
                "request_id": "req-1234",
                "title": "Reconcile",
                "body": "Cited note.\n",
                "source_refs": [],
            }
        )
    assert caught.value.code == "protocol_error"


def test_tools_cap_concurrent_reads_at_four_and_mutations_at_one():
    lock = threading.Lock()
    active_reads = 0
    max_reads = 0
    active_mutations = 0
    max_mutations = 0

    def impl(request: grokbot_cerebro.ExecRequest) -> grokbot_cerebro.PrivateExecResult:
        nonlocal active_reads, max_reads, active_mutations, max_mutations
        mutation = request.args[0] == "propose"
        with lock:
            if mutation:
                active_mutations += 1
                max_mutations = max(max_mutations, active_mutations)
            else:
                active_reads += 1
                max_reads = max(max_reads, active_reads)
        time.sleep(0.03)
        with lock:
            if mutation:
                active_mutations -= 1
            else:
                active_reads -= 1
        if mutation:
            title = request.args[request.args.index("--title") + 1]
            if title == "Fail":
                raise grokbot_cerebro.ChildFailure(1, b"", SECRET_STDERR.encode("utf-8"))
            return grokbot_cerebro.PrivateExecResult(
                stdout=json.dumps(
                    {
                        "schema": "cerebro-agents.proposal.v1",
                        "request_id": "req-1234",
                        "id": NOTE_ID,
                        "delivered": True,
                        "duplicate": False,
                        "phase": "terminal",
                        "delivery_state": "delivered",
                        "receipt_ref": "c" * 32,
                    }
                )
            )
        if request.args[0] == "show":
            return grokbot_cerebro.PrivateExecResult(
                stdout=json.dumps({"schema": "cerebro-agents.show.v1", "note": _note()})
            )
        if request.args[0] == "status":
            return grokbot_cerebro.PrivateExecResult(
                stdout=json.dumps(
                    {
                        "schema": "cerebro-agents.status.v1",
                        "request_id": "req-1234",
                        "proposal_id": NOTE_ID,
                        "phase": "in_progress",
                    }
                )
            )
        if request.args[0] == "doctor":
            return grokbot_cerebro.PrivateExecResult(
                stdout=json.dumps(
                    {
                        "schema": "cerebro-agents.doctor.v1",
                        "ok": True,
                        "summary": {"cli": "ok", "configuration": "ok", "index": "ok", "proposal_bridge": "ok"},
                    }
                )
            )
        return grokbot_cerebro.PrivateExecResult(
            stdout=json.dumps({"schema": "cerebro-agents.search.v1", "results": []})
        )

    tools, _ = _tools(impl)
    with ThreadPoolExecutor(max_workers=8) as pool:
        reads = list(
            pool.map(
                lambda item: item[0](item[1]),
                [
                    (tools.search, {"query": "one"}),
                    (tools.search, {"query": "two"}),
                    (tools.show, {"note_id": NOTE_ID}),
                    (tools.show, {"note_id": NOTE_ID}),
                    (tools.status, {"request_id": "req-1234"}),
                    (tools.status, {"request_id": "req-5678"}),
                    (tools.health, {}),
                    (tools.health, {}),
                ],
            )
        )
    assert len(reads) == 8
    assert max_reads == 4

    def propose(title: str) -> dict[str, object]:
        return tools.propose(
            {
                "request_id": "req-ok-12" if title != "Fail" else "req-fail1",
                "title": title,
                "body": "Cited note.\n",
                "source_refs": [],
            }
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        mutations = list(pool.map(lambda title: _call(propose, title), ["Fail", "Reconcile", "Reconcile"]))
    assert max_mutations == 1
    assert sum(1 for item in mutations if item is not None) == 2
    assert sum(1 for item in mutations if item is None) == 1


def _call(func, title: str) -> dict[str, object] | None:
    try:
        return func(title)
    except grokbot_cerebro.CerebroError:
        return None


def _cerebro_paths(tmp_path: Path) -> tuple[Path, Path]:
    workdir = tmp_path / "cerebro-work"
    workdir.mkdir()
    executable = tmp_path / "cerebro-agents"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable, workdir


def test_path_validation_rejects_relative_symlink_and_overlapping_paths(tmp_path: Path):
    executable, workdir = _cerebro_paths(tmp_path)
    assert grokbot_cerebro.validate_cli_executable(str(executable)) == str(executable)
    assert grokbot_cerebro.validate_workdir(str(workdir)) == str(workdir)
    with pytest.raises(grokbot_cerebro.CerebroError) as caught:
        grokbot_cerebro.validate_cli_executable("cerebro-agents")
    assert caught.value.code == "invalid_request"
    _assert_hidden(str(caught.value), "cerebro-agents")
    with pytest.raises(grokbot_cerebro.CerebroError):
        grokbot_cerebro.validate_cli_executable(str(workdir))
    with pytest.raises(grokbot_cerebro.CerebroError):
        grokbot_cerebro.validate_workdir(str(executable))
    if os.name == "posix":
        link = tmp_path / "linked-bin"
        link.symlink_to(executable)
        with pytest.raises(grokbot_cerebro.CerebroError) as caught:
            grokbot_cerebro.validate_cli_executable(str(link))
        _assert_hidden(str(caught.value), str(link), str(executable))


def test_doctor_and_canary_are_sanitized_and_unit_uses_brigade_cli(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", "not-a-real-token")
    executable, workdir = _cerebro_paths(tmp_path)
    grokbot_packs.apply_setup(
        tmp_path,
        "cerebro-memory",
        bearer_env="TEST_GROKBOT_BEARER",
        cli_executable=executable,
        workdir=workdir,
    )
    checks = grokbot_cerebro.doctor(tmp_path)
    canary = grokbot_cerebro.canary(tmp_path)
    rendered = json.dumps({"checks": checks, "canary": canary})
    assert "not-a-real-token" not in rendered
    assert str(executable) not in rendered
    assert str(workdir) not in rendered
    assert all(set(check) == {"check", "status"} for check in checks)
    assert canary["ok"] is False

    unit = grokbot_packs.render_install_service(tmp_path, "cerebro-memory")
    assert "-m" in unit
    assert "brigade" in unit
    assert "run" in unit
    assert "cloud" in unit
    assert "grokbot" in unit
    assert "serve" in unit
    assert "--pack" in unit
    assert "cerebro-memory" in unit
    assert "127.0.0.1:8770" in unit
    assert str(executable) not in unit
    assert "not-a-real-token" not in unit
    assert "node" not in unit.lower()
    assert "npm" not in unit.lower()


def _runner_request(
    tmp_path: Path,
    code: str,
    *,
    timeout_ms: int = 2000,
    max_buffer_bytes: int = 1024,
    stdin: bytes | None = None,
) -> grokbot_cerebro.ExecRequest:
    return grokbot_cerebro.ExecRequest(
        file=sys.executable,
        args=("-c", code),
        cwd=str(tmp_path),
        timeout_ms=timeout_ms,
        max_buffer_bytes=max_buffer_bytes,
        env=grokbot_cerebro.child_environment(),
        stdin=stdin,
    )


def _reap_pid(pid: int) -> None:
    try:
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except OSError:
        pass


def test_subprocess_runner_timeout_does_not_wait_on_pipe_holding_descendant(tmp_path: Path):
    pid_path = tmp_path / "descendant.pid"
    descendant = (
        f"import os,time; from pathlib import Path; Path({str(pid_path)!r}).write_text(str(os.getpid())); time.sleep(8)"
    )
    parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', {descendant!r}]); time.sleep(8)"
    started = time.monotonic()
    try:
        with pytest.raises(grokbot_cerebro.ChildTimeout):
            grokbot_cerebro._subprocess_runner(_runner_request(tmp_path, parent, timeout_ms=500))
        elapsed = time.monotonic() - started
        assert elapsed < 2.0
        assert elapsed >= 0.25
    finally:
        if pid_path.is_file():
            try:
                _reap_pid(int(pid_path.read_text(encoding="utf-8").strip()))
            except ValueError:
                pass


def test_subprocess_runner_detects_stdout_oversize_without_unbounded_buffer(tmp_path: Path):
    code = "import sys,time; sys.stdout.buffer.write(b'O' * 2048); sys.stdout.buffer.flush(); time.sleep(8)"
    started = time.monotonic()
    with pytest.raises(grokbot_cerebro.ChildOversize):
        grokbot_cerebro._subprocess_runner(_runner_request(tmp_path, code, timeout_ms=4000, max_buffer_bytes=1024))
    assert time.monotonic() - started < 2.0


def test_subprocess_runner_detects_stderr_oversize_without_unbounded_buffer(tmp_path: Path):
    code = "import sys,time; sys.stderr.buffer.write(b'E' * 2048); sys.stderr.buffer.flush(); time.sleep(8)"
    started = time.monotonic()
    with pytest.raises(grokbot_cerebro.ChildOversize):
        grokbot_cerebro._subprocess_runner(_runner_request(tmp_path, code, timeout_ms=4000, max_buffer_bytes=1024))
    assert time.monotonic() - started < 2.0


def test_subprocess_runner_keeps_bounded_streams_and_child_failure_mapping(tmp_path: Path):
    result = grokbot_cerebro._subprocess_runner(
        _runner_request(tmp_path, "import sys; sys.stdout.write('ok-out'); sys.stderr.write('ok-err')")
    )
    assert result.stdout == "ok-out"
    assert not hasattr(result, "stderr")

    with pytest.raises(grokbot_cerebro.ChildFailure) as failed:
        grokbot_cerebro._subprocess_runner(
            _runner_request(
                tmp_path,
                "import sys; sys.stdout.write('fail-out'); sys.stderr.write('idempotency conflict\\n'); raise SystemExit(2)",
            )
        )
    assert failed.value.exit_code == 2
    assert failed.value.stdout == b"fail-out"
    assert failed.value.stderr == b"idempotency conflict\n"

    with pytest.raises(grokbot_cerebro.CerebroError) as mapped:
        grokbot_cerebro.run_exec(
            _runner_request(
                tmp_path,
                "import sys; sys.stdout.write('fail-out'); sys.stderr.write('idempotency conflict\\n'); raise SystemExit(2)",
            )
        )
    assert mapped.value.code == "unavailable"
    assert mapped.value.message == "Cerebro observation is unavailable"
    retained = grokbot_cerebro._CHILD_FAILURES[mapped.value]
    assert retained[0] == 2
    assert retained[1] == "fail-out"
    assert retained[2] == "idempotency conflict\n"
    _assert_hidden(str(mapped.value))


def test_render_unit_restores_hardening_and_hides_private_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", "not-a-real-token")
    executable, workdir = _cerebro_paths(tmp_path)
    grokbot_packs.apply_setup(
        tmp_path,
        "cerebro-memory",
        bearer_env="TEST_GROKBOT_BEARER",
        cli_executable=executable,
        workdir=workdir,
    )
    unit = grokbot_cerebro.render_unit(tmp_path)
    assert "NoNewPrivileges=yes" in unit
    assert "PrivateTmp=yes" in unit
    assert "ProtectSystem=strict" in unit
    assert "ProtectHome=read-only" in unit
    assert str(executable) not in unit
    assert "not-a-real-token" not in unit


def test_render_unit_quotes_exact_workdir_read_write_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", "not-a-real-token")
    workdir = tmp_path / "cerebro work; echo 'hi' & *"
    workdir.mkdir()
    executable = tmp_path / "cerebro-agents"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    grokbot_packs.apply_setup(
        tmp_path,
        "cerebro-memory",
        bearer_env="TEST_GROKBOT_BEARER",
        cli_executable=executable,
        workdir=workdir,
    )
    unit = grokbot_cerebro.render_unit(tmp_path)
    quoted = grokbot_ops._systemd_quote(str(workdir))
    lines = [line for line in unit.splitlines() if line.startswith("ReadWritePaths=")]
    assert lines == [f"ReadWritePaths={quoted}"]
    assert quoted.startswith('"') and quoted.endswith('"')
    assert "ProtectSystem=strict" in unit
    assert "ProtectHome=read-only" in unit
    assert "NoNewPrivileges=yes" in unit
    assert "PrivateTmp=yes" in unit
    assert str(executable) not in unit
    assert "not-a-real-token" not in unit
    assert f"ReadWritePaths={grokbot_ops._systemd_quote(str(tmp_path))}" not in unit
    assert "ReadWritePaths=/" not in unit
    assert "ReadWritePaths=/home" not in unit
    assert "ReadWritePaths=~" not in unit


def _assert_unit_hardening_and_workdir(unit: str, *, workdir: Path, target: Path, secret: str) -> None:
    quoted = grokbot_ops._systemd_quote(str(workdir))
    lines = [line for line in unit.splitlines() if line.startswith("ReadWritePaths=")]
    assert lines == [f"ReadWritePaths={quoted}"]
    assert quoted.startswith('"') and quoted.endswith('"')
    assert "ProtectSystem=strict" in unit
    assert "ProtectHome=read-only" in unit
    assert "NoNewPrivileges=yes" in unit
    assert "PrivateTmp=yes" in unit
    assert secret not in unit
    assert f"ReadWritePaths={grokbot_ops._systemd_quote(str(target))}" not in unit
    assert "ReadWritePaths=/" not in unit
    assert "ReadWritePaths=/home" not in unit
    assert "ReadWritePaths=~" not in unit


def _forbid_bearer_resolution(monkeypatch: pytest.MonkeyPatch, bearer_file: Path | None = None) -> None:
    from brigade import grokbot_mcp

    def _boom(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("bearer must not be resolved while rendering a unit")

    monkeypatch.setattr(grokbot_ops, "_resolve_bearer", _boom)
    monkeypatch.setattr(grokbot_mcp, "load_bearer", _boom)
    if bearer_file is None:
        return
    real_read = Path.read_text

    def guarded_read(self: Path, *args: object, **kwargs: object) -> str:
        if Path(self).resolve() == bearer_file.resolve():
            raise AssertionError("secret file must not be read while rendering a unit")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)


def test_render_unit_env_bearer_succeeds_without_current_env_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    secret = "not-a-real-token"
    monkeypatch.delenv("TEST_GROKBOT_BEARER", raising=False)
    workdir = tmp_path / "cerebro work; echo 'hi' & *"
    workdir.mkdir()
    executable = tmp_path / "cerebro-agents"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    grokbot_packs.apply_setup(
        tmp_path,
        "cerebro-memory",
        bearer_env="TEST_GROKBOT_BEARER",
        cli_executable=executable,
        workdir=workdir,
    )
    checks = grokbot_cerebro.doctor(tmp_path)
    assert any(check["check"] == "config" and check["status"] == "fail" for check in checks)
    canary = grokbot_cerebro.canary(tmp_path)
    assert canary["ok"] is False
    assert canary.get("reason") == "config"
    _forbid_bearer_resolution(monkeypatch)
    unit = grokbot_cerebro.render_unit(tmp_path)
    assert "--bearer-env" in unit
    assert "TEST_GROKBOT_BEARER" in unit
    assert secret not in unit
    assert str(executable) not in unit
    _assert_unit_hardening_and_workdir(unit, workdir=workdir, target=tmp_path, secret=secret)
    written = grokbot_cerebro.write_unit(tmp_path, tmp_path / "units")
    assert written.read_text(encoding="utf-8") == unit


def test_render_unit_file_bearer_succeeds_without_current_secret_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bearer_fixture = "synthetic-bearer-fixture"
    bearer_file = tmp_path / "bearer.txt"
    workdir = tmp_path / "cerebro work; echo 'hi' & *"
    workdir.mkdir()
    executable = tmp_path / "cerebro-agents"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    grokbot_packs.apply_setup(
        tmp_path,
        "cerebro-memory",
        bearer_file=bearer_file,
        cli_executable=executable,
        workdir=workdir,
    )
    assert not bearer_file.exists()
    checks = grokbot_cerebro.doctor(tmp_path)
    assert any(check["check"] == "config" and check["status"] == "fail" for check in checks)
    canary = grokbot_cerebro.canary(tmp_path)
    assert canary["ok"] is False
    assert canary.get("reason") == "config"
    _forbid_bearer_resolution(monkeypatch, bearer_file)
    unit = grokbot_cerebro.render_unit(tmp_path)
    assert "--bearer-file" in unit
    assert str(bearer_file.resolve()) in unit
    assert bearer_fixture not in unit
    assert str(executable) not in unit
    _assert_unit_hardening_and_workdir(unit, workdir=workdir, target=tmp_path, secret=bearer_fixture)
    bearer_file.write_text(bearer_fixture + "\n", encoding="utf-8")
    bearer_file.chmod(0o600)
    unit_after = grokbot_cerebro.render_unit(tmp_path)
    assert unit_after == unit
    assert bearer_fixture not in unit_after
    written = grokbot_cerebro.write_unit(tmp_path, tmp_path / "units")
    assert written.read_text(encoding="utf-8") == unit
    assert bearer_fixture not in written.read_text(encoding="utf-8")


def test_cerebro_unit_uses_shared_listener_recovery_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", "not-a-real-token")
    executable, workdir = _cerebro_paths(tmp_path)
    grokbot_packs.apply_setup(
        tmp_path,
        "cerebro-memory",
        bearer_env="TEST_GROKBOT_BEARER",
        cli_executable=executable,
        workdir=workdir,
    )
    assert_listener_recovery_policy(grokbot_cerebro.render_unit(tmp_path))
